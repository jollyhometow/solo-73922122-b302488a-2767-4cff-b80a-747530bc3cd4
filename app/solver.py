"""时刻表出行求解与新增班次影响分析核心。

输入两份同一时间基准（整数秒）的时刻表、各站最短换乘秒数和出行调查需求，
对每条需求分别在旧表、新表上求**最早到达时间**及一套乘车方案，并标出
旧表按期、新表超期或不可达的受影响需求。

乘车规则
--------
* 乘客可在车站等待，首次上车无需换乘缓冲；
* 同一车次可跨站乘坐，同车停留不算换车；
* 换车须满足 ``接续车次发车 >= 到达时刻 + 本站最短换乘秒数``，等号可接上；
* 最多换车 2 次，即最多乘坐 3 个车次段（leg）。

求解采用按乘车段数分层的最早到达松弛：第 ``k`` 层的起点路径固定只含
``k`` 个车次段，因此第 3 层产生的路径至多含 3 个车次段、至多 2 次换车。
每层只从层首快照出发，保证换车次数约束不被层内更新绕过。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

_INF = 10**18
MAX_TRIPS = 20
MIN_STOPS = 2
MAX_STOPS = 12
MAX_DEMANDS = 30
MAX_LEGS = 3  # 最多换车 2 次 => 最多 3 个车次段


# ---------------------------------------------------------------------------
# 输入数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Stop:
    """一条停站记录：站名、到站秒、发车秒。"""

    station: str
    arrival: int
    departure: int


@dataclass(frozen=True)
class Trip:
    """一个车次：车次编号及按顺序排列的停站记录（允许重复停站）。"""

    trip_id: str
    stops: tuple[Stop, ...]


@dataclass(frozen=True)
class Timetable:
    """一份时刻表（旧表或新表）。"""

    label: str
    trips: tuple[Trip, ...]

    def stations(self) -> frozenset[str]:
        collected: set[str] = set()
        for trip in self.trips:
            for stop in trip.stops:
                collected.add(stop.station)
        return frozenset(collected)


@dataclass(frozen=True)
class Demand:
    """一条出行调查需求。"""

    demand_id: str
    origin: str
    destination: str
    departure_time: int
    arrival_deadline: int


# ---------------------------------------------------------------------------
# 输出数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Leg:
    """一个车次段：乘坐某车次从 board_stop_index 到 alight_stop_index。

    停站序号为 1-based，对应该车次 ``stops`` 数组中的位置（环线重复停站时
    靠序号区分同一站名的不同停站）。
    """

    trip_id: str
    board_stop_index: int
    alight_stop_index: int
    board_station: str
    alight_station: str
    board_departure: int
    alight_arrival: int
    transfer_buffer_seconds: int


@dataclass(frozen=True)
class Journey:
    """一套完整乘车方案。"""

    legs: tuple[Leg, ...]
    arrival_time: int

    @property
    def leg_count(self) -> int:
        return len(self.legs)

    @property
    def transfer_count(self) -> int:
        return max(0, len(self.legs) - 1)


@dataclass(frozen=True)
class ScenarioResult:
    """一条需求在单份时刻表上的求解结果。"""

    reachable: bool
    earliest_arrival: Optional[int]
    on_time: bool
    journey: Optional[Journey]


@dataclass(frozen=True)
class DemandAnalysis:
    """一条需求在新旧两份表上的对比结果。"""

    demand: Demand
    old: ScenarioResult
    new: ScenarioResult
    affected: bool
    impact_type: Optional[str]  # "late" | "unreachable" | None


# ---------------------------------------------------------------------------
# 构造与校验
# ---------------------------------------------------------------------------
def _require_nonneg_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须为整数秒")
    if value < 0:
        raise ValueError(f"{name} 不能为负")
    return value


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须为非空字符串")
    return value.strip()


def build_timetable(payload: Sequence[Mapping[str, object]], label: str) -> Timetable:
    """从字典载荷构造并校验一份时刻表。

    校验内容：车次 1..20 个、车次号非空且不重复、每车次 2..12 条停站、
    到站不晚于发车、本站发车不晚于下站到站。
    """

    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise ValueError(f"{label}必须为车次数组")
    if not 1 <= len(payload) <= MAX_TRIPS:
        raise ValueError(f"{label}车次数量必须在 1..{MAX_TRIPS} 之间")

    trips: list[Trip] = []
    seen_ids: set[str] = set()
    for raw_trip in payload:
        if not isinstance(raw_trip, Mapping):
            raise ValueError(f"{label}的每个车次必须为对象")
        trip_id = _require_text(raw_trip.get("trip_id"), f"{label}车次号")
        if trip_id in seen_ids:
            raise ValueError(f"{label}车次号重复：{trip_id}")
        seen_ids.add(trip_id)

        raw_stops = raw_trip.get("stops")
        if not isinstance(raw_stops, Sequence) or isinstance(raw_stops, (str, bytes)):
            raise ValueError(f"车次 {trip_id} 的 stops 必须为数组")
        if not MIN_STOPS <= len(raw_stops) <= MAX_STOPS:
            raise ValueError(
                f"车次 {trip_id} 的停站记录数量必须在 {MIN_STOPS}..{MAX_STOPS} 之间"
            )

        stops: list[Stop] = []
        for index, raw_stop in enumerate(raw_stops):
            if not isinstance(raw_stop, Mapping):
                raise ValueError(f"车次 {trip_id} 的每条停站记录必须为对象")
            station = _require_text(
                raw_stop.get("station"), f"车次 {trip_id} 第 {index + 1} 站站名"
            )
            arrival = _require_nonneg_int(
                raw_stop.get("arrival"), f"车次 {trip_id} 第 {index + 1} 站到站秒"
            )
            departure = _require_nonneg_int(
                raw_stop.get("departure"), f"车次 {trip_id} 第 {index + 1} 站发车秒"
            )
            if arrival > departure:
                raise ValueError(
                    f"车次 {trip_id} 第 {index + 1} 站到站秒 {arrival} 晚于发车秒 {departure}"
                )
            if stops and stops[-1].departure > arrival:
                raise ValueError(
                    f"车次 {trip_id} 第 {index} 站发车秒 {stops[-1].departure} "
                    f"晚于第 {index + 1} 站到站秒 {arrival}"
                )
            stops.append(Stop(station=station, arrival=arrival, departure=departure))
        trips.append(Trip(trip_id=trip_id, stops=tuple(stops)))

    return Timetable(label=label, trips=tuple(trips))


def build_transfers(payload: Mapping[str, object]) -> dict[str, int]:
    """构造站名 -> 最短换乘秒数映射，值须为非负整数。"""

    if not isinstance(payload, Mapping):
        raise ValueError("minimum_transfer_seconds 必须为站名到秒数的映射对象")
    transfers: dict[str, int] = {}
    for raw_station, raw_seconds in payload.items():
        station = _require_text(raw_station, "换乘站名")
        seconds = _require_nonneg_int(raw_seconds, f"车站 {station} 的最短换乘秒数")
        transfers[station] = seconds
    if not transfers:
        raise ValueError("minimum_transfer_seconds 至少包含一个车站")
    return transfers


def build_demands(payload: Sequence[Mapping[str, object]]) -> tuple[Demand, ...]:
    """构造并校验出行需求列表（1..30 条）。"""

    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise ValueError("demands 必须为需求数组")
    if not 1 <= len(payload) <= MAX_DEMANDS:
        raise ValueError(f"出行需求数量必须在 1..{MAX_DEMANDS} 之间")

    demands: list[Demand] = []
    seen_ids: set[str] = set()
    for raw in payload:
        if not isinstance(raw, Mapping):
            raise ValueError("每条出行需求必须为对象")
        demand_id = _require_text(raw.get("demand_id"), "需求编号")
        if demand_id in seen_ids:
            raise ValueError(f"需求编号重复：{demand_id}")
        seen_ids.add(demand_id)
        origin = _require_text(raw.get("origin"), f"需求 {demand_id} 起点站")
        destination = _require_text(raw.get("destination"), f"需求 {demand_id} 终到站")
        departure_time = _require_nonneg_int(
            raw.get("departure_time"), f"需求 {demand_id} 出发秒"
        )
        arrival_deadline = _require_nonneg_int(
            raw.get("arrival_deadline"), f"需求 {demand_id} 到达期限"
        )
        if departure_time > arrival_deadline:
            raise ValueError(
                f"需求 {demand_id} 出发秒 {departure_time} 晚于到达期限 {arrival_deadline}"
            )
        demands.append(
            Demand(
                demand_id=demand_id,
                origin=origin,
                destination=destination,
                departure_time=departure_time,
                arrival_deadline=arrival_deadline,
            )
        )
    return tuple(demands)


def validate_transfer_coverage(
    old_timetable: Timetable,
    new_timetable: Timetable,
    transfers: Mapping[str, int],
) -> None:
    """时刻表中出现的每个车站都必须给出最短换乘秒数。"""

    for timetable in (old_timetable, new_timetable):
        missing = sorted(timetable.stations() - transfers.keys())
        if missing:
            raise ValueError(
                f"{timetable.label}中的车站 {', '.join(missing)} 缺少最短换乘秒数"
            )


# ---------------------------------------------------------------------------
# 最早到达求解
# ---------------------------------------------------------------------------
def earliest_journey(
    timetable: Timetable,
    transfers: Mapping[str, int],
    origin: str,
    departure_time: int,
    destination: str,
) -> Optional[Journey]:
    """求单条需求在一份时刻表上的最早到达方案；不可达返回 None。

    状态为“某车站的最早可接续时刻”。按车次段数分 0..2 共三层扩展：
    第 0 层首次上车无缓冲，第 1、2 层上车须留足本站最短换乘时间。
    每层从层首快照扩展，保证任意方案至多 3 个车次段（2 次换车）。
    """

    # 站名 -> 可乘坐的 (车次下标, 停站下标) 列表，按输入顺序保持确定性。
    board_options: dict[str, list[tuple[int, int]]] = {}
    for trip_index, trip in enumerate(timetable.trips):
        for stop_index, stop in enumerate(trip.stops):
            board_options.setdefault(stop.station, []).append((trip_index, stop_index))

    # 起点在出发秒即可乘车；空路径表示起终相同、无需乘车。
    best: dict[str, int] = {origin: departure_time}
    paths: dict[str, tuple[Leg, ...]] = {origin: ()}

    for level in range(MAX_LEGS):
        snapshot_best = dict(best)
        snapshot_paths = dict(paths)
        for station, ready_time in snapshot_best.items():
            path = snapshot_paths[station]
            # 首次上车无需缓冲；换车须留足本站最短换乘时间，等号可接上。
            buffer_seconds = 0 if level == 0 else transfers[station]
            earliest_board = ready_time + buffer_seconds

            for trip_index, board_index in board_options.get(station, ()):
                trip = timetable.trips[trip_index]
                board_stop = trip.stops[board_index]
                if board_stop.departure < earliest_board:
                    continue

                # 上车后可在该车次的任一后续停站下车（同车跨站不算换车）。
                # 对每个到达站只保留最早到站记录；层内新写入的状态留待
                # 下一层处理，保证换车次数约束不被绕过。
                for alight_index in range(board_index + 1, len(trip.stops)):
                    alight_stop = trip.stops[alight_index]
                    arrival = alight_stop.arrival
                    if arrival >= best.get(alight_stop.station, _INF):
                        continue
                    leg = Leg(
                        trip_id=trip.trip_id,
                        board_stop_index=board_index + 1,
                        alight_stop_index=alight_index + 1,
                        board_station=station,
                        alight_station=alight_stop.station,
                        board_departure=board_stop.departure,
                        alight_arrival=arrival,
                        transfer_buffer_seconds=buffer_seconds,
                    )
                    best[alight_stop.station] = arrival
                    paths[alight_stop.station] = path + (leg, )

    if destination not in best:
        return None
    return Journey(legs=paths[destination], arrival_time=best[destination])


def _scenario(
    timetable: Timetable,
    transfers: Mapping[str, int],
    demand: Demand,
) -> ScenarioResult:
    journey = earliest_journey(
        timetable,
        transfers,
        demand.origin,
        demand.departure_time,
        demand.destination,
    )
    if journey is None:
        return ScenarioResult(
            reachable=False,
            earliest_arrival=None,
            on_time=False,
            journey=None,
        )
    return ScenarioResult(
        reachable=True,
        earliest_arrival=journey.arrival_time,
        on_time=journey.arrival_time <= demand.arrival_deadline,
        journey=journey,
    )


def analyze(
    old_timetable: Timetable,
    new_timetable: Timetable,
    transfers: Mapping[str, int],
    demands: Sequence[Demand],
) -> tuple[DemandAnalysis, ...]:
    """对每条需求分别在旧表、新表上求解并标记调整影响。

    受影响（affected）定义：旧表按期可达，而新表超期到达或完全不可达。
    """

    results: list[DemandAnalysis] = []
    for demand in demands:
        old_result = _scenario(old_timetable, transfers, demand)
        new_result = _scenario(new_timetable, transfers, demand)

        affected = old_result.on_time and not new_result.on_time
        if not affected:
            impact_type: Optional[str] = None
        elif not new_result.reachable:
            impact_type = "unreachable"
        else:
            impact_type = "late"

        results.append(
            DemandAnalysis(
                demand=demand,
                old=old_result,
                new=new_result,
                affected=affected,
                impact_type=impact_type,
            )
        )
    return tuple(results)
