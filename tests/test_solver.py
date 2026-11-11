"""时刻表最早到达求解器的单元测试。

除针对规则的构造用例外，还在随机小型时刻表上用独立的穷举实现
（枚举至多 3 个车次段的全部乘降组合）交叉验证最早到达与可达性。
"""

import random
import unittest
from itertools import product

from app.solver import (
    Demand,
    Timetable,
    build_demands,
    build_timetable,
    build_transfers,
    earliest_journey,
    validate_transfer_coverage,
)


def tt(label, trips_raw):
    return build_timetable(trips_raw, label)


LINE_TIMETABLE = tt(
    "直线",
    [
        {
            "trip_id": "T1",
            "stops": [
                {"station": "A", "arrival": 0, "departure": 0},
                {"station": "B", "arrival": 100, "departure": 110},
                {"station": "C", "arrival": 200, "departure": 200},
            ],
        },
        {
            "trip_id": "T2",
            "stops": [
                {"station": "B", "arrival": 170, "departure": 170},
                {"station": "D", "arrival": 260, "departure": 260},
            ],
        },
    ],
)
TRANSFERS = {"A": 30, "B": 60, "C": 30, "D": 30}


class BasicJourneyTests(unittest.TestCase):
    def test_single_trip_direct_ride(self):
        journey = earliest_journey(LINE_TIMETABLE, TRANSFERS, "A", 0, "C")
        self.assertIsNotNone(journey)
        self.assertEqual(journey.arrival_time, 200)
        self.assertEqual(journey.transfer_count, 0)
        self.assertEqual(len(journey.legs), 1)
        leg = journey.legs[0]
        self.assertEqual(leg.trip_id, "T1")
        self.assertEqual((leg.board_stop_index, leg.alight_stop_index), (1, 3))
        self.assertEqual(leg.transfer_buffer_seconds, 0)

    def test_passenger_can_wait_at_origin(self):
        # 出发秒 50 早于 T1 在 B 站的发车 110，等待后仍可乘车。
        journey = earliest_journey(LINE_TIMETABLE, TRANSFERS, "B", 50, "C")
        self.assertIsNotNone(journey)
        self.assertEqual(journey.arrival_time, 200)
        self.assertEqual(journey.legs[0].board_departure, 110)

    def test_missed_departure_is_unreachable_without_alternatives(self):
        # 111 秒才到 B 站，T1 已于 110 秒发车，T2 不到 C。
        journey = earliest_journey(LINE_TIMETABLE, TRANSFERS, "B", 111, "C")
        self.assertIsNone(journey)

    def test_same_origin_and_destination_is_zero_leg(self):
        journey = earliest_journey(LINE_TIMETABLE, TRANSFERS, "A", 42, "A")
        self.assertIsNotNone(journey)
        self.assertEqual(journey.arrival_time, 42)
        self.assertEqual(journey.legs, ())
        self.assertEqual(journey.transfer_count, 0)

    def test_transfer_buffer_equality_just_makes_it(self):
        # T1 于 100 秒到 B，100+60=160；T2 在 170 发车，可接续。
        journey = earliest_journey(LINE_TIMETABLE, TRANSFERS, "A", 0, "D")
        self.assertIsNotNone(journey)
        self.assertEqual(journey.arrival_time, 260)
        self.assertEqual(journey.transfer_count, 1)
        self.assertEqual([leg.trip_id for leg in journey.legs], ["T1", "T2"])
        self.assertEqual(journey.legs[1].transfer_buffer_seconds, 60)
        # 等号可接上：把 T2 发车提前到 160 仍能接上。
        tight = tt(
            "临界",
            [
                {
                    "trip_id": "T1",
                    "stops": [
                        {"station": "A", "arrival": 0, "departure": 0},
                        {"station": "B", "arrival": 100, "departure": 110},
                    ],
                },
                {
                    "trip_id": "T2",
                    "stops": [
                        {"station": "B", "arrival": 160, "departure": 160},
                        {"station": "D", "arrival": 260, "departure": 260},
                    ],
                },
            ],
        )
        ok = earliest_journey(tight, TRANSFERS, "A", 0, "D")
        self.assertIsNotNone(ok)
        self.assertEqual(ok.arrival_time, 260)
        # 提前 1 秒则赶不上。
        broken = tt(
            "断接",
            [
                {
                    "trip_id": "T1",
                    "stops": [
                        {"station": "A", "arrival": 0, "departure": 0},
                        {"station": "B", "arrival": 100, "departure": 110},
                    ],
                },
                {
                    "trip_id": "T2",
                    "stops": [
                        {"station": "B", "arrival": 159, "departure": 159},
                        {"station": "D", "arrival": 260, "departure": 260},
                    ],
                },
            ],
        )
        self.assertIsNone(earliest_journey(broken, TRANSFERS, "A", 0, "D"))


class RingLineTests(unittest.TestCase):
    RING = tt(
        "环线",
        [
            {
                "trip_id": "L1",
                "stops": [
                    {"station": "A", "arrival": 0, "departure": 0},
                    {"station": "B", "arrival": 100, "departure": 110},
                    {"station": "C", "arrival": 200, "departure": 210},
                    {"station": "A", "arrival": 300, "departure": 310},
                    {"station": "B", "arrival": 400, "departure": 410},
                ],
            }
        ],
    )
    RING_TRANSFERS = {"A": 60, "B": 60, "C": 60}

    def test_ring_second_loop_uses_later_stop_index(self):
        # B -> A 必须乘到第二圈的第 4 停站。
        journey = earliest_journey(self.RING, self.RING_TRANSFERS, "B", 60, "A")
        self.assertIsNotNone(journey)
        self.assertEqual(journey.arrival_time, 300)
        leg = journey.legs[0]
        self.assertEqual(leg.trip_id, "L1")
        self.assertEqual((leg.board_stop_index, leg.alight_stop_index), (2, 4))

    def test_ring_earliest_occurrence_is_chosen(self):
        journey = earliest_journey(self.RING, self.RING_TRANSFERS, "A", 0, "B")
        self.assertEqual(journey.arrival_time, 100)
        self.assertEqual(
            (journey.legs[0].board_stop_index, journey.legs[0].alight_stop_index),
            (1, 2),
        )


class TransferLimitTests(unittest.TestCase):
    """最多换车 2 次（最多 3 个车次段）的约束。"""

    NETWORK = tt(
        "链式网络",
        [
            {
                "trip_id": "T1",
                "stops": [
                    {"station": "S0", "arrival": 0, "departure": 0},
                    {"station": "S1", "arrival": 100, "departure": 100},
                ],
            },
            {
                "trip_id": "T2",
                "stops": [
                    {"station": "S1", "arrival": 200, "departure": 200},
                    {"station": "S2", "arrival": 300, "departure": 300},
                ],
            },
            {
                "trip_id": "T3",
                "stops": [
                    {"station": "S2", "arrival": 400, "departure": 400},
                    {"station": "S3", "arrival": 500, "departure": 500},
                ],
            },
            {
                "trip_id": "T4",
                "stops": [
                    {"station": "S3", "arrival": 600, "departure": 600},
                    {"station": "S4", "arrival": 700, "departure": 700},
                ],
            },
        ],
    )
    TRANSFERS = {f"S{i}": 0 for i in range(5)}

    def test_three_legs_two_transfers_allowed(self):
        journey = earliest_journey(self.NETWORK, self.TRANSFERS, "S0", 0, "S3")
        self.assertIsNotNone(journey)
        self.assertEqual(journey.arrival_time, 500)
        self.assertEqual(journey.transfer_count, 2)
        self.assertEqual([leg.trip_id for leg in journey.legs], ["T1", "T2", "T3"])

    def test_fourth_transfer_is_not_allowed(self):
        # S0 -> S4 需要 3 次换车，超出上限。
        self.assertIsNone(
            earliest_journey(self.NETWORK, self.TRANSFERS, "S0", 0, "S4")
        )

    def test_staying_on_same_trip_is_not_a_transfer(self):
        local = tt(
            "长线",
            [
                {
                    "trip_id": "T1",
                    "stops": [
                        {"station": "S0", "arrival": 0, "departure": 0},
                        {"station": "S1", "arrival": 100, "departure": 100},
                        {"station": "S2", "arrival": 200, "departure": 200},
                        {"station": "S3", "arrival": 300, "departure": 300},
                    ],
                }
            ],
        )
        journey = earliest_journey(local, self.TRANSFERS, "S0", 0, "S3")
        self.assertEqual(journey.transfer_count, 0)
        self.assertEqual(len(journey.legs), 1)


class ExpressOvertakeTests(unittest.TestCase):
    def test_express_train_skipping_stations_is_faster(self):
        network = tt(
            "快慢车",
            [
                {
                    "trip_id": "慢车",
                    "stops": [
                        {"station": "A", "arrival": 0, "departure": 0},
                        {"station": "B", "arrival": 100, "departure": 130},
                        {"station": "C", "arrival": 300, "departure": 300},
                    ],
                },
                {
                    "trip_id": "快车",
                    "stops": [
                        {"station": "A", "arrival": 10, "departure": 20},
                        {"station": "C", "arrival": 200, "departure": 200},
                    ],
                },
            ],
        )
        transfers = {"A": 0, "B": 0, "C": 0}
        journey = earliest_journey(network, transfers, "A", 0, "C")
        self.assertEqual(journey.arrival_time, 200)
        self.assertEqual(journey.legs[0].trip_id, "快车")


# ---------------------------------------------------------------------------
# 独立穷举参考实现
# ---------------------------------------------------------------------------
def brute_force_earliest(timetable: Timetable, transfers, origin, departure_time, destination):
    """枚举深度 1..3 的全部车次段组合，返回最早到达秒或 None。"""
    if origin == destination:
        return departure_time

    options = []  # (trip, board_index, alight_index)
    for trip in timetable.trips:
        stops = trip.stops
        for board_index in range(len(stops)):
            for alight_index in range(board_index + 1, len(stops)):
                options.append((trip, board_index, alight_index))

    best = None
    for depth in (1, 2, 3):
        for sequence in product(options, repeat=depth):
            current_station = origin
            ready_time = departure_time
            for position, (trip, board_index, alight_index) in enumerate(sequence):
                board_stop = trip.stops[board_index]
                if board_stop.station != current_station:
                    break
                buffer_seconds = 0 if position == 0 else transfers[current_station]
                if board_stop.departure < ready_time + buffer_seconds:
                    break
                alight_stop = trip.stops[alight_index]
                current_station = alight_stop.station
                ready_time = alight_stop.arrival
            else:
                if current_station == destination:
                    if best is None or ready_time < best:
                        best = ready_time
    return best


def _random_timetable(rng, label):
    station_names = ["A", "B", "C", "D"]
    trips = []
    for trip_index in range(rng.randint(1, 4)):
        stop_count = rng.randint(2, 6)
        clock = rng.randint(0, 50)
        stops = []
        for _ in range(stop_count):
            arrival = clock
            departure = arrival + rng.randint(0, 20)
            stops.append(
                {
                    "station": rng.choice(station_names),
                    "arrival": arrival,
                    "departure": departure,
                }
            )
            clock = departure + rng.randint(0, 40)
        trips.append({"trip_id": f"{label}-{trip_index}", "stops": stops})
    return build_timetable(trips, label)


class BruteForceCrossCheckTests(unittest.TestCase):
    def test_random_timetables_match_brute_force(self):
        rng = random.Random(20261111)
        stations = ["A", "B", "C", "D"]
        transfers = {name: rng.choice([0, 10, 30, 60]) for name in stations}
        checked = 0
        for seed in range(180):
            rng.seed(20261111 + seed)
            timetable = _random_timetable(rng, "随机表")
            for _ in range(4):
                origin = rng.choice(stations)
                destination = rng.choice(stations)
                departure_time = rng.randint(0, 400)
                expected = brute_force_earliest(
                    timetable, transfers, origin, departure_time, destination
                )
                journey = earliest_journey(
                    timetable, transfers, origin, departure_time, destination
                )
                actual = None if journey is None else journey.arrival_time
                self.assertEqual(
                    actual,
                    expected,
                    msg=f"seed={seed} {origin}->{destination} @ {departure_time}",
                )
                if journey is not None:
                    # 方案自身必须满足全部乘车规则。
                    self.assertLessEqual(journey.transfer_count, 2)
                    self._assert_journey_valid(
                        journey, transfers, origin, departure_time, destination
                    )
                checked += 1
        self.assertGreater(checked, 300)

    def _assert_journey_valid(self, journey, transfers, origin, departure_time, destination):
        if not journey.legs:
            # 起终相同：空方案，到达时间即出发时间。
            self.assertEqual(origin, destination)
            self.assertEqual(journey.arrival_time, departure_time)
            return
        for position, leg in enumerate(journey.legs):
            if position == 0:
                self.assertEqual(leg.board_station, origin)
                self.assertGreaterEqual(leg.board_departure, departure_time)
                self.assertEqual(leg.transfer_buffer_seconds, 0)
            else:
                previous = journey.legs[position - 1]
                self.assertEqual(previous.alight_station, leg.board_station)
                self.assertGreaterEqual(
                    leg.board_departure,
                    previous.alight_arrival + transfers[leg.board_station],
                )
            self.assertLess(leg.board_stop_index, leg.alight_stop_index)
        self.assertEqual(journey.legs[-1].alight_station, destination)
        self.assertEqual(journey.arrival_time, journey.legs[-1].alight_arrival)


class ValidationTests(unittest.TestCase):
    def test_arrival_after_departure_rejected(self):
        with self.assertRaises(ValueError):
            build_timetable(
                [
                    {
                        "trip_id": "T1",
                        "stops": [
                            {"station": "A", "arrival": 10, "departure": 5},
                            {"station": "B", "arrival": 20, "departure": 20},
                        ],
                    }
                ],
                "坏表",
            )

    def test_departure_after_next_arrival_rejected(self):
        with self.assertRaises(ValueError):
            build_timetable(
                [
                    {
                        "trip_id": "T1",
                        "stops": [
                            {"station": "A", "arrival": 0, "departure": 99},
                            {"station": "B", "arrival": 50, "departure": 60},
                        ],
                    }
                ],
                "坏表",
            )

    def test_duplicate_trip_id_and_size_limits(self):
        with self.assertRaises(ValueError):
            build_timetable(
                [
                    {"trip_id": "T1", "stops": [
                        {"station": "A", "arrival": 0, "departure": 0},
                        {"station": "B", "arrival": 1, "departure": 1},
                    ]},
                    {"trip_id": "T1", "stops": [
                        {"station": "A", "arrival": 2, "departure": 2},
                        {"station": "B", "arrival": 3, "departure": 3},
                    ]},
                ],
                "重复",
            )
        with self.assertRaises(ValueError):
            build_timetable([], "空表")
        with self.assertRaises(ValueError):
            build_timetable(
                [
                    {
                        "trip_id": "T1",
                        "stops": [
                            {"station": "A", "arrival": 0, "departure": 0}
                        ],
                    }
                ],
                "单站",
            )

    def test_transfer_coverage_and_demand_building(self):
        transfers = build_transfers({"A": 0, "B": 30})
        with self.assertRaises(ValueError):
            validate_transfer_coverage(LINE_TIMETABLE, LINE_TIMETABLE, transfers)
        full = build_transfers({"A": 0, "B": 30, "C": 0, "D": 0})
        validate_transfer_coverage(LINE_TIMETABLE, LINE_TIMETABLE, full)

        demands = build_demands(
            [
                {
                    "demand_id": "D1",
                    "origin": "A",
                    "destination": "C",
                    "departure_time": 0,
                    "arrival_deadline": 200,
                }
            ]
        )
        self.assertIsInstance(demands[0], Demand)
        with self.assertRaises(ValueError):
            build_demands(
                [
                    {
                        "demand_id": "D2",
                        "origin": "A",
                        "destination": "C",
                        "departure_time": 10,
                        "arrival_deadline": 9,
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
