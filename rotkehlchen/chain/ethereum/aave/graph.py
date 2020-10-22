import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional, Set, Tuple

from eth_utils.address import to_checksum_address

from rotkehlchen.accounting.structures import Balance
from rotkehlchen.assets.asset import Asset, EthereumToken
from rotkehlchen.chain.ethereum.aave.common import (
    AAVE_RESERVE_TO_ASSET,
    ASSET_TO_AAVE_RESERVE_ADDRESS,
    AaveBalances,
    AaveHistory,
    AaveInquirer,
    _get_reserve_address_decimals,
)
from rotkehlchen.chain.ethereum.graph import Graph
from rotkehlchen.chain.ethereum.makerdao.common import RAY
from rotkehlchen.chain.ethereum.structures import (
    AaveBorrowEvent,
    AaveEvent,
    AaveLiquidationEvent,
    AaveRepayEvent,
    AaveSimpleEvent,
)
from rotkehlchen.chain.ethereum.utils import token_normalized_value
from rotkehlchen.constants.ethereum import ATOKEN_ABI
from rotkehlchen.constants.misc import ZERO
from rotkehlchen.errors import UnknownAsset
from rotkehlchen.fval import FVal
from rotkehlchen.history.price import query_usd_price_zero_if_error
from rotkehlchen.inquirer import Inquirer
from rotkehlchen.premium.premium import Premium
from rotkehlchen.typing import ChecksumEthAddress, Timestamp
from rotkehlchen.user_messages import MessagesAggregator
from rotkehlchen.utils.misc import ts_now

if TYPE_CHECKING:
    from rotkehlchen.chain.ethereum.manager import EthereumManager
    from rotkehlchen.db.dbhandler import DBHandler

log = logging.getLogger(__name__)

AAVE_GRAPH_RECENT_SECS = 600  # 10 mins

USER_RESERVES_QUERY = """
{{
  userReserves(where: {{ user: "{address}"}}) {{
    id
    reserve{{
      id
      symbol
    }}
    user {{
      id
    }}
  }}
}}"""


DEPOSIT_EVENTS_QUERY = """
  deposits (orderBy: timestamp, orderDirection: asc, where: {
   user: $address, timestamp_lte: $end_ts, timestamp_gte: $start_ts
  }) {
    id
    amount
    referrer {
      id
    }
    reserve {
      id
    }
    timestamp
  }
}
"""

USER_EVENTS_QUERY = """
  users (where: {id: $address}) {
    id
    depositHistory {
        id
        amount
        reserve {
          id
        }
        timestamp
    }
    redeemUnderlyingHistory {
        id
        amount
        reserve {
          id
        }
        timestamp
    }
    borrowHistory {
        id
        amount
        reserve {
          id
        }
        timestamp
        borrowRate
        borrowRateMode
        accruedBorrowInterest
    }
    repayHistory {
        id
        amountAfterFee
        fee
        reserve {
          id
        }
        timestamp
    }
    liquidationCallHistory {
        id
        collateralAmount
        collateralReserve {
          id
        }
        principalAmount
        principalReserve {
          id
        }
        timestamp
    }
    reserves{
        id
        aTokenBalanceHistory {
          id
          balance
          userBalanceIndex
          interestRedirectionAddress
          redirectedBalance
          timestamp
        }
    }
  }
}
"""


class ATokenBalanceHistory(NamedTuple):
    reserve_address: ChecksumEthAddress
    balance: FVal
    tx_hash: str
    timestamp: Timestamp


class AaveUserReserve(NamedTuple):
    address: ChecksumEthAddress
    symbol: str


def _calculate_loss(
        borrow_actions: List[AaveEvent],
        balances: AaveBalances,
) -> Dict[Asset, Balance]:
    borrow_actions.sort(key=lambda event: event.timestamp)
    historical_borrow_balances: Dict[Asset, FVal] = defaultdict(FVal)
    total_lost: Dict[Asset, Balance] = defaultdict(Balance)

    for b_action in borrow_actions:
        if b_action.event_type == 'borrow':
            historical_borrow_balances[b_action.asset] -= b_action.value.amount  # type: ignore
        elif b_action.event_type == 'repay':
            historical_borrow_balances[b_action.asset] += b_action.value.amount  # type: ignore
        elif b_action.event_type == 'liquidation':
            # At liquidation you lose the collateral asset
            total_lost[b_action.collateral_asset] += b_action.collateral_balance  # type: ignore  # noqa: E501
            # And your principal asset is repaid
            historical_borrow_balances[b_action.principal_asset] += b_action.principal_balance.amount  # type: ignore # noqa: E501

    for b_asset, amount in historical_borrow_balances.items():
        borrow_balance = balances.borrowing.get(b_asset.identifier, None)
        if borrow_balance is not None:
            amount += borrow_balance.balance.amount

        usd_price = Inquirer().find_usd_price(b_asset)
        total_lost[b_asset] = Balance(
            # add total_lost amount in case of liquidations
            amount=total_lost[b_asset].amount + amount,
            usd_value=amount * usd_price,
        )

    return total_lost


def _parse_common_event_data(
        entry: Dict[str, Any],
        from_ts: Timestamp,
        to_ts: Timestamp,
) -> Optional[Tuple[Timestamp, str, int]]:
    """Parses and returns the common data of each event.

    Returns None if timestamp is out of range or if there is an error
    """
    timestamp = entry['timestamp']
    if timestamp < from_ts or timestamp > to_ts:
        # Since for the user data we can't query per timestamp, filter timestamps here
        return None

    pair = entry['id'].split(':')
    if len(pair) != 2:
        log.error(
            f'Could not parse the id entry for an aave liquidation as '
            f'returned by graph: {entry["id"]}.  Skipping entry ...',
        )
        return None

    tx_hash = pair[0]
    index = int(pair[1])  # not really log index
    return timestamp, tx_hash, index


def _parse_atoken_balance_history(
        history: List[Dict[str, Any]],
        from_ts: Timestamp,
        to_ts: Timestamp,
) -> List[ATokenBalanceHistory]:
    result = []
    for entry in history:
        timestamp = entry['timestamp']
        if timestamp < from_ts or timestamp > to_ts:
            continue

        entry_id = entry['id']
        pairs = entry_id.split('0x')
        if len(pairs) != 4:
            log.error(
                f'Expected to find 3 hashes in graps\'s aTokenBalanceHistory '
                f'id but the encountered id does not match: {entry_id}. Skipping entry...',
            )
            continue

        reserve_address = to_checksum_address('0x' + pairs[2])
        tx_hash = '0x' + pairs[3]
        asset = AAVE_RESERVE_TO_ASSET.get(reserve_address, None)
        if asset is None:
            log.error(
                f'Unknown aave reserve address returned by atoken balance history '
                f' graph query: {reserve_address}. Skipping entry ...',
            )
            continue

        _, decimals = _get_reserve_address_decimals(asset.identifier)
        balance = token_normalized_value(int(entry['balance']), token_decimals=decimals)
        result.append(ATokenBalanceHistory(
            reserve_address=reserve_address,
            balance=balance,
            tx_hash=tx_hash,
            timestamp=timestamp,
        ))

    return result


def _get_reserve_asset_and_decimals(
        entry: Dict[str, Any],
        reserve_key: str,
) -> Optional[Tuple[Asset, int]]:
    reserve_address = to_checksum_address(entry[reserve_key]['id'])
    asset = AAVE_RESERVE_TO_ASSET.get(reserve_address, None)
    if asset is None:
        log.error(
            f'Unknown aave reserve address returned by graph query: '
            f'{reserve_address}. Skipping entry ...',
        )
        return None

    _, decimals = _get_reserve_address_decimals(asset.identifier)
    return asset, decimals


class AaveGraphInquirer(AaveInquirer):
    """Reads Aave historical data from the graph protocol"""

    def __init__(
            self,
            ethereum_manager: 'EthereumManager',
            database: 'DBHandler',
            msg_aggregator: MessagesAggregator,
            premium: Optional[Premium],
    ) -> None:
        """May raise RemoteError if we can't connect to the subgraph"""
        super().__init__(
            ethereum_manager=ethereum_manager,
            database=database,
            premium=premium,
            msg_aggregator=msg_aggregator,
        )
        self.graph = Graph('https://api.thegraph.com/subgraphs/name/aave/protocol-raw')

    def get_history_for_addresses(
            self,
            addresses: List[ChecksumEthAddress],
            to_block: int,
            from_timestamp: Timestamp,
            to_timestamp: Timestamp,
            aave_balances: Dict[ChecksumEthAddress, AaveBalances],
    ) -> Dict[ChecksumEthAddress, AaveHistory]:
        """
        Queries aave history for a list of addresses.

        This function should be entered while holding the history_lock
        semaphore
        """
        result = {}
        for address in addresses:
            history_results = self.get_history_for_address(
                user_address=address,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                balances=aave_balances.get(address, AaveBalances({}, {})),
            )
            if history_results is None:
                continue
            result[address] = history_results

        return result

    def _get_user_reserves(self, address: ChecksumEthAddress) -> List[AaveUserReserve]:
        query = self.graph.query(
            querystr=USER_RESERVES_QUERY.format(address=address.lower()),
        )
        result = []
        for entry in query['userReserves']:
            reserve = entry['reserve']
            result.append(AaveUserReserve(
                address=to_checksum_address(reserve['id']),
                symbol=reserve['symbol'],
            ))

        return result

    def _calculate_interest_and_profit(
            self,
            user_address: ChecksumEthAddress,
            user_result: Dict[str, Any],
            actions: List[AaveSimpleEvent],
            balances: AaveBalances,
            db_interest_events: Set[AaveSimpleEvent],
            from_ts: Timestamp,
            to_ts: Timestamp,
    ) -> Tuple[List[AaveSimpleEvent], Dict[EthereumToken, Balance]]:
        reserve_history = {}
        for reserve in user_result['reserves']:
            pairs = reserve['id'].split('0x')
            if len(pairs) != 3:
                log.error(
                    f'Expected to find 2 hashes in graph\'s reserve history id '
                    f'but the encountered id does not match: {reserve["id"]}. Skipping entry...',
                )
            reserve_address = to_checksum_address('0x' + pairs[2])
            atoken_history = _parse_atoken_balance_history(
                history=reserve['aTokenBalanceHistory'],
                from_ts=from_ts,
                to_ts=to_ts,
            )
            reserve_history[reserve_address] = atoken_history

        interest_events: List[AaveSimpleEvent] = []
        atoken_balances: Dict[Asset, FVal] = defaultdict(FVal)
        used_history_indices = set()
        total_earned: Dict[EthereumToken, Balance] = defaultdict(Balance)

        # Go through the existing db interest events and add total earned
        for interest_event in db_interest_events:
            total_earned[interest_event.asset] += interest_event.value

        # Create all new interest events in the query
        actions.sort(key=lambda event: event.timestamp)
        for action in actions:
            if action.event_type == 'deposit':
                atoken_balances[action.asset] += action.value.amount
            else:  # withdrawal
                atoken_balances[action.asset] -= action.value.amount

            reserve_address = ASSET_TO_AAVE_RESERVE_ADDRESS.get(action.asset.identifier, None)  # type: ignore  # noqa: E501
            history = reserve_history.get(reserve_address, None)
            if history is None:
                log.error(
                    f'Could not find aTokenBalanceHistory for reserve '
                    f'{reserve_address} in an aave graph response.'
                    f' Skipping entry...',
                )
                continue
            history.sort(key=lambda event: event.timestamp)

            for idx, entry in enumerate(history):
                if idx in used_history_indices:
                    continue
                used_history_indices.add(idx)

                if entry.tx_hash == action.tx_hash:
                    diff = entry.balance - atoken_balances[action.asset]
                    if diff != ZERO:
                        atoken_balances[action.asset] = entry.balance
                        try:
                            asset = EthereumToken('a' + action.asset.identifier)
                        except UnknownAsset:
                            log.error(
                                f'Could not find corresponding aToken to'
                                f'{action.asset.identifier} during an aave graph uery'
                                f' Skipping entry...',
                            )
                            continue
                        timestamp = entry.timestamp
                        usd_price = query_usd_price_zero_if_error(
                            asset=asset,
                            time=timestamp,
                            location='aave interest event from graph query',
                            msg_aggregator=self.msg_aggregator,
                        )
                        earned_balance = Balance(amount=diff, usd_value=diff * usd_price)
                        interest_event = AaveSimpleEvent(
                            event_type='interest',
                            asset=asset,
                            value=earned_balance,
                            block_number=0,  # can't get from graph query
                            timestamp=timestamp,
                            tx_hash=entry.tx_hash,
                            # not really the log index, but should also be unique
                            log_index=action.log_index + 1,
                        )
                        if interest_event in db_interest_events:
                            # This should not really happen since we already query
                            # historical atoken balance history in the new range
                            log.warning(
                                f'During aave subgraph query interest and profit calculation '
                                f'tried to generate interest event {interest_event} that '
                                f'already existed in the DB ',
                            )
                            continue

                        interest_events.append(interest_event)
                        total_earned[asset] += earned_balance

                    # and once done break off the loop
                    break

                else:
                    # this atoken history is not due to an action, so skip it
                    # it's probably due to a simple transfer
                    atoken_balances[action.asset] = entry.balance
                    if action.event_type == 'deposit':
                        atoken_balances[action.asset] += action.value.amount
                    else:  # withdrawal
                        atoken_balances[action.asset] -= action.value.amount

        # Take aave unpaid interest into account
        for symbol, lending_balance in balances.lending.items():
            atoken = EthereumToken('a' + symbol)
            principal_balance = self.ethereum.call_contract(
                contract_address=atoken.ethereum_address,
                abi=ATOKEN_ABI,
                method_name='principalBalanceOf',
                arguments=[user_address],
            )
            unpaid_interest = lending_balance.balance.amount - (principal_balance / (FVal(10) ** FVal(atoken.decimals)))  # noqa: E501
            usd_price = Inquirer().find_usd_price(atoken)
            total_earned[atoken] += Balance(
                amount=unpaid_interest,
                usd_value=unpaid_interest * usd_price,
            )

        return interest_events, total_earned

    def _process_events(
            self,
            user_address: ChecksumEthAddress,
            user_result: Dict[str, Any],
            from_ts: Timestamp,
            to_ts: Timestamp,
            deposits: List[AaveSimpleEvent],
            withdrawals: List[AaveSimpleEvent],
            borrows: List[AaveBorrowEvent],
            repays: List[AaveRepayEvent],
            liquidations: List[AaveLiquidationEvent],
            db_events: List[AaveEvent],
            balances: AaveBalances,
    ) -> Tuple[List[AaveSimpleEvent], Dict[EthereumToken, Balance], Dict[Asset, Balance]]:
        """Calculates the interest events and the total earned from all the given events.
        Also calculates total loss from borrowing and liquidations.

        Also returns the edited DB events
        """
        actions: List[AaveSimpleEvent] = []
        borrow_actions: List[AaveEvent] = []
        db_interest_events: Set[AaveSimpleEvent] = set()
        for db_event in db_events:
            if db_event.event_type == 'deposit':
                actions.append(db_event)  # type: ignore
            elif db_event.event_type == 'withdrawal':
                actions.append(db_event)  # type: ignore
            elif db_event.event_type == 'interest':
                db_interest_events.add(db_event)  # type: ignore
            elif db_event.event_type == 'borrow':
                borrow_actions.append(db_event)
            elif db_event.event_type == 'repay':
                borrow_actions.append(db_event)
            elif db_event.event_type == 'liquidation':
                borrow_actions.append(db_event)

        interest_events, total_earned = self._calculate_interest_and_profit(
            user_address=user_address,
            user_result=user_result,
            actions=actions + deposits + withdrawals,
            balances=balances,
            db_interest_events=db_interest_events,
            from_ts=from_ts,
            to_ts=to_ts,
        )
        total_lost = _calculate_loss(
            borrow_actions=borrow_actions + borrows + repays + liquidations,  # type: ignore
            balances=balances,
        )

        return interest_events, total_earned, total_lost

    def _get_user_data(
            self,
            from_ts: Timestamp,
            to_ts: Timestamp,
            address: ChecksumEthAddress,
            balances: AaveBalances,
    ) -> AaveHistory:
        last_query = self.database.get_used_query_range(f'aave_events_{address}')
        db_events = self.database.get_aave_events(address=address)

        now = ts_now()
        last_query_ts = 0
        if last_query is not None:
            last_query_ts = last_query[1]
            from_ts = Timestamp(last_query_ts + 1)

        deposits = withdrawals = borrows = repays = liquidation_calls = []
        query = self.graph.query(
            querystr=USER_EVENTS_QUERY,
            param_types={'$address': 'ID!'},
            param_values={'address': address.lower()},
        )
        user_result = query['users'][0]
        if now - last_query_ts > AAVE_GRAPH_RECENT_SECS:
            # In theory if these were individual queries we should do them only if
            # we have not queried recently. In practise since we only do 1 query above
            # this is useless for now, but keeping the mechanism in case we change
            # the way we query the subgraph
            deposits = self._parse_deposits(user_result['depositHistory'], from_ts, to_ts)
            withdrawals = self._parse_withdrawals(
                withdrawals=user_result['redeemUnderlyingHistory'],
                from_ts=from_ts,
                to_ts=to_ts,
            )
            borrows = self._parse_borrows(user_result['borrowHistory'], from_ts, to_ts)
            repays = self._parse_repays(user_result['repayHistory'], from_ts, to_ts)
            liquidation_calls = self._parse_liquidations(
                user_result['liquidationCallHistory'],
                from_ts,
                to_ts,
            )

        interest_events, total_earned, total_lost = self._process_events(
            user_address=address,
            user_result=user_result,
            from_ts=from_ts,
            to_ts=to_ts,
            deposits=deposits,
            withdrawals=withdrawals,
            borrows=borrows,
            repays=repays,
            liquidations=liquidation_calls,
            db_events=db_events,
            balances=balances,
        )

        # Add all new events to the DB
        new_events: List[AaveEvent] = deposits + withdrawals + interest_events + borrows + repays + liquidation_calls  # type: ignore  # noqa: E501
        self.database.add_aave_events(address, new_events)
        # After all events have been queried then also update the query range.
        # Even if no events are found for an address we need to remember the range
        self.database.update_used_query_range(
            name=f'aave_events_{address}',
            start_ts=0,
            end_ts=now,
        )

        # Sort actions so that actions with same time are sorted deposit -> interest -> withdrawal
        all_events: List[AaveEvent] = new_events + db_events
        sort_map = {'deposit': 0, 'interest': 0.1, 'withdrawal': 0.2, 'borrow': 0.3, 'repay': 0.4, 'liquidation': 0.5}  # noqa: E501
        all_events.sort(key=lambda event: sort_map[event.event_type] + event.timestamp)
        return AaveHistory(
            events=all_events,
            total_earned=total_earned,
            total_lost=total_lost,
        )

    def _parse_deposits(
            self,
            deposits: List[Dict[str, Any]],
            from_ts: Timestamp,
            to_ts: Timestamp,
    ) -> List[AaveSimpleEvent]:
        events: List[AaveSimpleEvent] = []
        for entry in deposits:
            common = _parse_common_event_data(entry, from_ts, to_ts)
            if common is None:
                continue  # either timestamp out of range or error (logged in the function above)
            timestamp, tx_hash, index = common
            result = self._get_asset_and_balance(
                entry=entry,
                timestamp=timestamp,
                reserve_key='reserve',
                amount_key='amount',
                location='aave deposit from graph query',
            )
            if result is None:
                continue  # problem parsing, error already logged
            asset, balance = result
            events.append(AaveSimpleEvent(
                event_type='deposit',
                asset=asset,
                value=balance,
                block_number=0,  # can't get from graph query
                timestamp=timestamp,
                tx_hash=tx_hash,
                log_index=index,  # not really the log index, but should also be unique
            ))

        return events

    def _parse_withdrawals(
            self,
            withdrawals: List[Dict[str, Any]],
            from_ts: Timestamp,
            to_ts: Timestamp,
    ) -> List[AaveSimpleEvent]:
        events = []
        for entry in withdrawals:
            common = _parse_common_event_data(entry, from_ts, to_ts)
            if common is None:
                continue  # either timestamp out of range or error (logged in the function above)
            timestamp, tx_hash, index = common
            result = self._get_asset_and_balance(
                entry=entry,
                timestamp=timestamp,
                reserve_key='reserve',
                amount_key='amount',
                location='aave withdrawal from graph query',
            )
            if result is None:
                continue  # problem parsing, error already logged
            asset, balance = result
            events.append(AaveSimpleEvent(
                event_type='withdrawal',
                asset=asset,
                value=balance,
                block_number=0,  # can't get from graph query
                timestamp=timestamp,
                tx_hash=tx_hash,
                log_index=index,  # not really the log index, but should also be unique
            ))

        return events

    def _parse_borrows(
            self,
            borrows: List[Dict[str, Any]],
            from_ts: Timestamp,
            to_ts: Timestamp,
    ) -> List[AaveBorrowEvent]:
        events = []
        for entry in borrows:
            common = _parse_common_event_data(entry, from_ts, to_ts)
            if common is None:
                continue  # either timestamp out of range or error (logged in the function above)
            timestamp, tx_hash, index = common
            result = self._get_asset_and_balance(
                entry=entry,
                timestamp=timestamp,
                reserve_key='reserve',
                amount_key='amount',
                location='aave borrow from graph query',
            )
            if result is None:
                continue  # problem parsing, error already logged
            asset, balance = result
            borrow_rate = FVal(entry['borrowRate']) / RAY
            borrow_rate_mode = entry['borrowRateMode']
            accrued_borrow_interest = entry['accruedBorrowInterest']
            events.append(AaveBorrowEvent(
                event_type='borrow',
                asset=asset,
                value=balance,
                borrow_rate_mode=borrow_rate_mode.lower(),
                borrow_rate=borrow_rate,
                accrued_borrow_interest=accrued_borrow_interest,
                block_number=0,  # can't get from graph query
                timestamp=timestamp,
                tx_hash=tx_hash,
                log_index=index,  # not really the log index, but should also be unique
            ))

        return events

    def _parse_repays(
            self,
            repays: List[Dict[str, Any]],
            from_ts: Timestamp,
            to_ts: Timestamp,
    ) -> List[AaveRepayEvent]:
        events = []
        for entry in repays:
            common = _parse_common_event_data(entry, from_ts, to_ts)
            if common is None:
                continue  # either timestamp out of range or error (logged in the function above)
            timestamp, tx_hash, index = common
            result = _get_reserve_asset_and_decimals(entry, reserve_key='reserve')
            if result is None:
                continue  # problem parsing, error already logged
            asset, decimals = result
            amount_after_fee = token_normalized_value(
                int(entry['amountAfterFee']),
                token_decimals=decimals,
            )
            fee = token_normalized_value(int(entry['fee']), token_decimals=decimals)
            usd_price = query_usd_price_zero_if_error(
                asset=asset,
                time=timestamp,
                location='aave repay from graph query',
                msg_aggregator=self.msg_aggregator,
            )
            events.append(AaveRepayEvent(
                event_type='repay',
                asset=asset,
                value=Balance(amount=amount_after_fee, usd_value=amount_after_fee * usd_price),
                fee=Balance(amount=fee, usd_value=fee * usd_price),
                block_number=0,  # can't get from graph query
                timestamp=timestamp,
                tx_hash=tx_hash,
                log_index=index,  # not really the log index, but should also be unique
            ))

        return events

    def _parse_liquidations(
            self,
            liquidations: List[Dict[str, Any]],
            from_ts: Timestamp,
            to_ts: Timestamp,
    ) -> List[AaveLiquidationEvent]:
        events = []
        for entry in liquidations:
            common = _parse_common_event_data(entry, from_ts, to_ts)
            if common is None:
                continue  # either timestamp out of range or error (logged in the function above)
            timestamp, tx_hash, index = common
            result = self._get_asset_and_balance(
                entry=entry,
                timestamp=timestamp,
                reserve_key='collateralReserve',
                amount_key='collateralAmount',
                location='aave liquidation from graph query',
            )
            if result is None:
                continue  # problem parsing, error already logged
            collateral_asset, collateral_balance = result

            result = self._get_asset_and_balance(
                entry=entry,
                timestamp=timestamp,
                reserve_key='principalReserve',
                amount_key='principalAmount',
                location='aave liquidation from graph query',
            )
            if result is None:
                continue  # problem parsing, error already logged
            principal_asset, principal_balance = result
            events.append(AaveLiquidationEvent(
                event_type='liquidation',
                collateral_asset=collateral_asset,
                collateral_balance=collateral_balance,
                principal_asset=principal_asset,
                principal_balance=principal_balance,
                block_number=0,  # can't get from graph query
                timestamp=timestamp,
                tx_hash=tx_hash,
                log_index=index,  # not really the log index, but should also be unique
            ))

        return events

    def get_history_for_address(
            self,
            user_address: ChecksumEthAddress,
            from_timestamp: Timestamp,
            to_timestamp: Timestamp,
            balances: AaveBalances,
    ) -> Optional[AaveHistory]:
        """
        Queries aave history for a single address.

        This function should be entered while holding the history_lock
        semaphore
        """
        reserves = self._get_user_reserves(address=user_address)
        if len(reserves) != 0:
            return self._get_user_data(
                from_ts=from_timestamp,
                to_ts=to_timestamp,
                address=user_address,
                balances=balances,
            )

        return None

    def _get_asset_and_balance(
            self,
            entry: Dict[str, Any],
            timestamp: Timestamp,
            reserve_key: str,
            amount_key: str,
            location: str,
    ) -> Optional[Tuple[Asset, Balance]]:
        """Utility function to parse asset from graph query amount and price and return balance"""
        result = _get_reserve_asset_and_decimals(entry, reserve_key)
        if result is None:
            return None
        asset, decimals = result
        amount = token_normalized_value(
            token_amount=int(entry[amount_key]),
            token_decimals=decimals,
        )
        usd_price = query_usd_price_zero_if_error(
            asset=asset,
            time=timestamp,
            location=location,
            msg_aggregator=self.msg_aggregator,
        )
        return asset, Balance(amount=amount, usd_value=amount * usd_price)
