from copy import deepcopy
from itertools import chain, zip_longest
from logging import Logger, getLogger
from typing import Dict, Iterable, List, Tuple

from sortedcontainers import SortedList

from orderbook.order import Order
from orderbook.orderbookrecord import OrderBookRecord
from orderbook.transaction import Transaction


class OrderBook:
    sell: SortedList
    buy: SortedList
    timestamp: int
    __logger: Logger

    def __init__(self, logger=getLogger()):
        self.timestamp = 0
        self.buy = SortedList()
        self.sell = SortedList()
        self.__logger = logger

    def __repr__(self):
        return str([list(self.buy), list(self.sell)])

    def __str__(self):
        rows = []
        rows.append(
            "+-----------------------------------------------------------------+"
        )
        rows.append(
            "| BUY                            | SELL                           |"
        )
        rows.append(
            "| Id       | Volume      | Price | Price | Volume      | Id       |"
        )
        rows.append(
            "+----------+-------------+-------+-------+-------------+----------+"
        )

        for row in zip_longest(self.buy, self.sell):
            buy: OrderBookRecord = row[0]
            sell: OrderBookRecord = row[1]
            columns = [""]
            if buy is None:
                columns += [" " * 10, " " * 13, " " * 7]
            else:
                columns += [
                    f"{buy.order_id:10}",
                    f"{buy.current_peak_size:13,}",
                    f"{buy.price:7,}",
                ]

            if sell is None:
                columns += [" " * 7, " " * 13, " " * 10]
            else:
                columns += [
                    f"{sell.price:7,}",
                    f"{sell.current_peak_size:13,}",
                    f"{sell.order_id:10}",
                ]
            columns.append("")
            row = "|".join(columns)
            if len(row) != 67:
                self.__logger.warning(
                    "Order book line doesn't comply with pretty print constraints"
                )
            rows.append(row)

        rows.append(
            "+-----------------------------------------------------------------+"
        )
        return "\n".join(rows)

    def add(self, order: Order) -> List[Transaction]:
        self.timestamp += 1
        order = deepcopy(order)
        for record in chain(self.buy, self.sell):
            if order.order_id == record.order_id:
                self.__logger.error(f"Updating orders is prohibited: {order}")
                return []

        transactions = self.__try_to_fill_an_order(order)
        if order.quantity:
            side = self.buy if order.is_buy else self.sell
            record = OrderBookRecord(order, self.timestamp)
            side.add(record)
            self.__logger.info(f"Record inserted: {record}")
        else:
            self.__logger.info(f"{order} was completely executed")
        return transactions

    def __try_to_fill_an_order(self, order: Order) -> List[Transaction]:
        against = self.sell if order.is_buy else self.buy

        transactions: Dict[Tuple[int, int], int] = dict()
        candidate_records: List[OrderBookRecord] = list(
            filter(lambda x: self.__is_good_price(order, x), against)
        )

        for price in self.__unique([x.price for x in candidate_records]):
            if order.quantity == 0:
                break
            records = list(filter(lambda x: x.price == price, candidate_records))

            self.__fill_visible_peak_sizes(order, records, transactions)
            self.__fill_hidden_iceberg_orders(order, records, transactions)
            self.__fix_empty_records(records)

        against = filter(lambda x: x.quantity != 0, against)
        if order.is_buy:
            self.sell = SortedList(against)
        else:
            self.buy = SortedList(against)

        res: List[Transaction] = []
        for ((record_id, price), volume) in transactions.items():
            sell_id = order.order_id
            buy_id = record_id
            if order.is_buy:
                buy_id, sell_id = sell_id, buy_id

            res.append(Transaction(buy_id, sell_id, price, volume))
            self.__logger.info(f"Transaction: {repr(res[-1])}")

        return res

    @staticmethod
    def __is_good_price(order: Order, record: OrderBookRecord) -> bool:
        if order.is_buy:
            return order.price >= record.price
        else:
            return order.price <= record.price

    @staticmethod
    def __unique(lst) -> list:
        res = []
        for i in lst:
            if i not in res:
                res.append(i)
        return res

    def __fill_visible_peak_sizes(
        self,
        order: Order,
        records: Iterable[OrderBookRecord],
        transactions: Dict[Tuple[int, int], int],
    ) -> None:
        for record in records:
            if order.quantity == 0:
                break
            filled_quantity = min(record.current_peak_size, order.quantity)
            record.current_peak_size -= filled_quantity
            record.quantity -= filled_quantity

            order.quantity -= filled_quantity

            transactions[(record.order_id, record.price)] = filled_quantity
            self.__logger.debug(
                f"{order} filled by visible {record}. Volume {filled_quantity}"
            )

    def __fill_hidden_iceberg_orders(
        self,
        order: Order,
        records: Iterable[OrderBookRecord],
        transactions: Dict[Tuple[int, int], int],
    ) -> None:
        for record in records:
            if order.quantity == 0:
                break

            if record.quantity > order.quantity:
                max_peak = record.max_peak_size
                record.current_peak_size = min(
                    record.max_peak_size - order.quantity % max_peak,
                    record.quantity - order.quantity,
                )
                filled_quantity = order.quantity
            else:
                filled_quantity = record.quantity

            record.quantity -= filled_quantity
            order.quantity -= filled_quantity
            record.timestamp = self.timestamp

            transactions[(record.order_id, record.price)] += filled_quantity
            if filled_quantity != 0:
                self.__logger.debug(
                    f"{order} filled by hidden {record}. Volume {filled_quantity}"
                )

    def __fix_empty_records(self, records: Iterable[OrderBookRecord]) -> None:
        for order_priority, record in enumerate(records):
            assert record.quantity >= 0
            if record.current_peak_size == 0:
                record.current_peak_size = min(record.quantity, record.max_peak_size)
                record.timestamp = self.timestamp
                record.order_priority = order_priority
                if record.quantity != 0:
                    self.__logger.debug(f"Peak updated: {record}")
