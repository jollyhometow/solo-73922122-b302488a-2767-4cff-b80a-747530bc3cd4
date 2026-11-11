"""新增班次影响分析 FastAPI 服务。

接收运营导出的调整前（旧表）、调整后（新表）两份时刻表、各站最短换乘
秒数及最多 30 条出行调查需求，分别求每条需求的最早到达时间与乘车方案，
并标出旧表按期而新表超期或不可达的受影响需求。

本地启动：
    uvicorn app.main:app --host 0.0.0.0 --port 8000

交互文档：http://localhost:8000/docs
"""

from __future__ import annotations

import json
from typing import Dict, List, Literal, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .solver import (
    MAX_DEMANDS,
    MAX_STOPS,
    MAX_TRIPS,
    Demand,
    DemandAnalysis,
    Journey,
    Leg,
    ScenarioResult,
    Timetable,
    analyze,
    build_demands,
    build_timetable,
    build_transfers,
    validate_transfer_coverage,
)

# ---------------------------------------------------------------------------
# 示例一：环线。同一车次重复停靠 A、B，停站序号用于区分同一站名的不同停站。
# 新表整体后移 120 秒：D1、D2 由按期变为超期，D3 仍可按期（允许等待）。
# ---------------------------------------------------------------------------
RING_EXAMPLE_REQUEST = {
    "old_timetable": {
        "label": "调整前环线",
        "trips": [
            {
                "trip_id": "环线L1",
                "stops": [
                    {"station": "A站", "arrival": 0, "departure": 0},
                    {"station": "B站", "arrival": 100, "departure": 110},
                    {"station": "C站", "arrival": 200, "departure": 210},
                    {"station": "D站", "arrival": 300, "departure": 310},
                    {"station": "A站", "arrival": 400, "departure": 410},
                    {"station": "B站", "arrival": 500, "departure": 510},
                ],
            }
        ],
    },
    "new_timetable": {
        "label": "调整后环线",
        "trips": [
            {
                "trip_id": "环线L1",
                "stops": [
                    {"station": "A站", "arrival": 120, "departure": 120},
                    {"station": "B站", "arrival": 220, "departure": 230},
                    {"station": "C站", "arrival": 320, "departure": 330},
                    {"station": "D站", "arrival": 420, "departure": 430},
                    {"station": "A站", "arrival": 520, "departure": 530},
                    {"station": "B站", "arrival": 620, "departure": 630},
                ],
            }
        ],
    },
    "minimum_transfer_seconds": {
        "A站": 60,
        "B站": 60,
        "C站": 60,
        "D站": 60,
    },
    "demands": [
        {
            "demand_id": "R1",
            "origin": "A站",
            "destination": "B站",
            "departure_time": 0,
            "arrival_deadline": 150,
        },
        {
            "demand_id": "R2",
            "origin": "B站",
            "destination": "A站",
            "departure_time": 60,
            "arrival_deadline": 450,
        },
        {
            "demand_id": "R3",
            "origin": "C站",
            "destination": "D站",
            "departure_time": 200,
            "arrival_deadline": 500,
        },
    ],
}
RING_EXAMPLE_EXPLANATION = (
    "环线 L1 依次停靠 A→B→C→D→A→B，A 站、B 站重复停站，方案以停站序号区分。"
    "新表整体后移 120 秒：R1（A→B）旧表 100 秒到达、新表 220 秒超期；"
    "R2（B→A，乘至第二圈 A 站第 5 停站）旧表 400 秒到达、新表 520 秒超期；"
    "R3（C→D）新表虽推迟但仍在期限 500 内，420 秒到达，不受影响。"
)

# ---------------------------------------------------------------------------
# 示例二：换乘临界。P 站最短换乘 60 秒，等号可接上。
# 旧表 T2 在 P 站 380 秒发车，T1 于 300 秒到达，300+60=360<=380 可接续；
# 新表 T2 提前到 350 秒发车，360>350 无法接续，X→Z 变为不可达。
# ---------------------------------------------------------------------------
TRANSFER_EXAMPLE_REQUEST = {
    "old_timetable": {
        "label": "调整前",
        "trips": [
            {
                "trip_id": "T1",
                "stops": [
                    {"station": "X站", "arrival": 0, "departure": 0},
                    {"station": "P站", "arrival": 300, "departure": 320},
                    {"station": "Y站", "arrival": 600, "departure": 600},
                ],
            },
            {
                "trip_id": "T2",
                "stops": [
                    {"station": "P站", "arrival": 380, "departure": 380},
                    {"station": "Z站", "arrival": 500, "departure": 500},
                ],
            },
        ],
    },
    "new_timetable": {
        "label": "调整后",
        "trips": [
            {
                "trip_id": "T1",
                "stops": [
                    {"station": "X站", "arrival": 0, "departure": 0},
                    {"station": "P站", "arrival": 300, "departure": 320},
                    {"station": "Y站", "arrival": 600, "departure": 600},
                ],
            },
            {
                "trip_id": "T2",
                "stops": [
                    {"station": "P站", "arrival": 350, "departure": 350},
                    {"station": "Z站", "arrival": 470, "departure": 470},
                ],
            },
        ],
    },
    "minimum_transfer_seconds": {
        "X站": 30,
        "P站": 60,
        "Y站": 30,
        "Z站": 30,
    },
    "demands": [
        {
            "demand_id": "M1",
            "origin": "X站",
            "destination": "Z站",
            "departure_time": 0,
            "arrival_deadline": 500,
        },
        {
            "demand_id": "M2",
            "origin": "X站",
            "destination": "Y站",
            "departure_time": 0,
            "arrival_deadline": 600,
        },
    ],
}
TRANSFER_EXAMPLE_EXPLANATION = (
    "P 站最短换乘 60 秒。旧表 T1 于 300 秒到达 P 站，360 秒即可接续，"
    "T2 在 380 秒发车（等号可接上），M1 经 1 次换乘于 500 秒按期到达 Z 站；"
    "新表 T2 提前至 350 秒发车，360>350 赶不上，且无后续班次，M1 变为不可达。"
    "M2 全程乘坐 T1，两表均 600 秒按期到达，不受影响。"
)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class StopModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    station: str = Field(..., min_length=1, description="站名")
    arrival: int = Field(..., ge=0, description="到站秒（同一基准整数秒）")
    departure: int = Field(..., ge=0, description="发车秒")


class TripModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trip_id: str = Field(..., min_length=1, description="车次编号，同一时刻表内不重复")
    stops: List[StopModel] = Field(
        ...,
        min_length=2,
        max_length=MAX_STOPS,
        description="停站记录 2..12 条，允许重复停站；停站序号从 1 开始",
    )

    @model_validator(mode="after")
    def validate_time_order(self) -> "TripModel":
        for index, stop in enumerate(self.stops):
            if stop.arrival > stop.departure:
                raise ValueError(
                    f"车次 {self.trip_id} 第 {index + 1} 站到站秒 {stop.arrival} "
                    f"晚于发车秒 {stop.departure}"
                )
            if index > 0 and self.stops[index - 1].departure > stop.arrival:
                raise ValueError(
                    f"车次 {self.trip_id} 第 {index} 站发车秒 "
                    f"{self.stops[index - 1].departure} 晚于第 {index + 1} 站到站秒 "
                    f"{stop.arrival}"
                )
        return self


class TimetableModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field("时刻表", min_length=1, description="时刻表标签，便于区分新旧表")
    trips: List[TripModel] = Field(
        ..., min_length=1, max_length=MAX_TRIPS, description="车次 1..20 个"
    )

    @model_validator(mode="after")
    def validate_trip_ids_unique(self) -> "TimetableModel":
        ids = [trip.trip_id for trip in self.trips]
        duplicates = sorted({trip_id for trip_id in ids if ids.count(trip_id) > 1})
        if duplicates:
            raise ValueError(f"{self.label}车次号重复：{', '.join(duplicates)}")
        return self


class DemandModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    demand_id: str = Field(..., min_length=1, description="需求编号，请求内不重复")
    origin: str = Field(..., min_length=1, description="起点站")
    destination: str = Field(..., min_length=1, description="终到站")
    departure_time: int = Field(..., ge=0, description="出发秒")
    arrival_deadline: int = Field(..., ge=0, description="到达期限秒")

    @model_validator(mode="after")
    def validate_deadline(self) -> "DemandModel":
        if self.departure_time > self.arrival_deadline:
            raise ValueError(
                f"需求 {self.demand_id} 出发秒 {self.departure_time} "
                f"晚于到达期限 {self.arrival_deadline}"
            )
        return self


class ImpactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_timetable: TimetableModel = Field(..., description="调整前时刻表")
    new_timetable: TimetableModel = Field(..., description="调整后时刻表")
    minimum_transfer_seconds: Dict[str, int] = Field(
        ...,
        min_length=1,
        description="各站最短换乘秒数；时刻表中出现的每个车站都必须提供",
    )
    demands: List[DemandModel] = Field(
        ..., min_length=1, max_length=MAX_DEMANDS, description="出行调查需求 1..30 条"
    )

    @model_validator(mode="after")
    def validate_transfer_coverage_and_demand_stations(self) -> "ImpactRequest":
        for key, value in self.minimum_transfer_seconds.items():
            if not key.strip():
                raise ValueError("minimum_transfer_seconds 的站名不能为空")
            if value < 0:
                raise ValueError(f"车站 {key} 的最短换乘秒数不能为负")
        covered = set(self.minimum_transfer_seconds)
        for timetable in (self.old_timetable, self.new_timetable):
            stations = {
                stop.station for trip in timetable.trips for stop in trip.stops
            }
            missing = sorted(stations - covered)
            if missing:
                raise ValueError(
                    f"{timetable.label}中的车站 {', '.join(missing)} 缺少最短换乘秒数"
                )
        known_stations = (
            {
                stop.station
                for trip in self.old_timetable.trips
                for stop in trip.stops
            }
            | {
                stop.station
                for trip in self.new_timetable.trips
                for stop in trip.stops
            }
        )
        for demand in self.demands:
            if demand.origin not in known_stations:
                raise ValueError(f"需求 {demand.demand_id} 起点站 {demand.origin} 不在任何时刻表中")
            if demand.destination not in known_stations:
                raise ValueError(
                    f"需求 {demand.demand_id} 终到站 {demand.destination} 不在任何时刻表中"
                )
        return self


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------
class LegOut(BaseModel):
    trip_id: str
    board_stop_index: int = Field(..., description="上车站在该车次停站记录中的 1-based 序号")
    alight_stop_index: int = Field(..., description="下车站 1-based 序号")
    board_station: str
    alight_station: str
    board_departure: int
    alight_arrival: int
    transfer_buffer_seconds: int = Field(
        ..., description="上车前使用的换乘缓冲秒数；首次上车为 0"
    )


class JourneyOut(BaseModel):
    legs: List[LegOut]
    arrival_time: int
    leg_count: int
    transfer_count: int


class ScenarioOut(BaseModel):
    reachable: bool
    earliest_arrival: Optional[int]
    on_time: bool = Field(..., description="最早到达是否不晚于到达期限")
    journey: Optional[JourneyOut]


class DemandResultOut(BaseModel):
    demand_id: str
    origin: str
    destination: str
    departure_time: int
    arrival_deadline: int
    old: ScenarioOut
    new: ScenarioOut
    affected: bool = Field(
        ..., description="是否受影响：旧表按期可达，而新表超期或不可达"
    )
    impact_type: Optional[Literal["late", "unreachable"]]


class ImpactSummary(BaseModel):
    total_demands: int
    affected_count: int
    affected_late_count: int
    affected_unreachable_count: int
    old_on_time_count: int
    new_on_time_count: int
    old_unreachable_count: int
    new_unreachable_count: int


class ImpactResponse(BaseModel):
    status: Literal["ok"] = "ok"
    message: str
    summary: ImpactSummary
    results: List[DemandResultOut]


# ---------------------------------------------------------------------------
# 数据转换
# ---------------------------------------------------------------------------
def _leg_out(leg: Leg) -> LegOut:
    return LegOut(
        trip_id=leg.trip_id,
        board_stop_index=leg.board_stop_index,
        alight_stop_index=leg.alight_stop_index,
        board_station=leg.board_station,
        alight_station=leg.alight_station,
        board_departure=leg.board_departure,
        alight_arrival=leg.alight_arrival,
        transfer_buffer_seconds=leg.transfer_buffer_seconds,
    )


def _journey_out(journey: Optional[Journey]) -> Optional[JourneyOut]:
    if journey is None:
        return None
    return JourneyOut(
        legs=[_leg_out(leg) for leg in journey.legs],
        arrival_time=journey.arrival_time,
        leg_count=journey.leg_count,
        transfer_count=journey.transfer_count,
    )


def _scenario_out(result: ScenarioResult) -> ScenarioOut:
    return ScenarioOut(
        reachable=result.reachable,
        earliest_arrival=result.earliest_arrival,
        on_time=result.on_time,
        journey=_journey_out(result.journey),
    )


def _build_domain_objects(
    payload: ImpactRequest,
) -> tuple[Timetable, Timetable, dict[str, int], tuple[Demand, ...]]:
    old_timetable = build_timetable(
        [trip.model_dump(mode="json") for trip in payload.old_timetable.trips],
        payload.old_timetable.label,
    )
    new_timetable = build_timetable(
        [trip.model_dump(mode="json") for trip in payload.new_timetable.trips],
        payload.new_timetable.label,
    )
    transfers = build_transfers(payload.minimum_transfer_seconds)
    validate_transfer_coverage(old_timetable, new_timetable, transfers)
    demands = build_demands(
        [demand.model_dump(mode="json") for demand in payload.demands]
    )
    return old_timetable, new_timetable, transfers, demands


def _response(payload: ImpactRequest, analyses: tuple[DemandAnalysis, ...]) -> ImpactResponse:
    results: list[DemandResultOut] = []
    for analysis in analyses:
        results.append(
            DemandResultOut(
                demand_id=analysis.demand.demand_id,
                origin=analysis.demand.origin,
                destination=analysis.demand.destination,
                departure_time=analysis.demand.departure_time,
                arrival_deadline=analysis.demand.arrival_deadline,
                old=_scenario_out(analysis.old),
                new=_scenario_out(analysis.new),
                affected=analysis.affected,
                impact_type=analysis.impact_type,
            )
        )

    summary = ImpactSummary(
        total_demands=len(analyses),
        affected_count=sum(item.affected for item in analyses),
        affected_late_count=sum(item.impact_type == "late" for item in analyses),
        affected_unreachable_count=sum(
            item.impact_type == "unreachable" for item in analyses
        ),
        old_on_time_count=sum(item.old.on_time for item in analyses),
        new_on_time_count=sum(item.new.on_time for item in analyses),
        old_unreachable_count=sum(not item.old.reachable for item in analyses),
        new_unreachable_count=sum(not item.new.reachable for item in analyses),
    )
    message = (
        f"共分析 {summary.total_demands} 条出行需求：{summary.affected_count} 条受影响"
        f"（超期 {summary.affected_late_count} 条、不可达 {summary.affected_unreachable_count} 条），"
        "受影响定义为旧表按期可达而新表超期或不可达。"
    )
    return ImpactResponse(message=message, summary=summary, results=results)


def _solve(payload: ImpactRequest) -> ImpactResponse:
    old_timetable, new_timetable, transfers, demands = _build_domain_objects(payload)
    analyses = analyze(old_timetable, new_timetable, transfers, demands)
    return _response(payload, analyses)


def _example_response(request_payload: dict) -> dict:
    return _solve(ImpactRequest.model_validate(request_payload)).model_dump(mode="json")


RING_EXAMPLE_RESPONSE = _example_response(RING_EXAMPLE_REQUEST)
TRANSFER_EXAMPLE_RESPONSE = _example_response(TRANSFER_EXAMPLE_REQUEST)

app = FastAPI(
    title="新增班次影响分析 API",
    summary="时刻表调整前后最早到达与接驳影响分析服务",
    description=(
        "接收调整前、调整后两份时刻表（各至多 20 个车次、每车次 2..12 条停站）、"
        "各站最短换乘秒数及至多 30 条出行调查需求。对每条需求分别在两份时刻表上"
        "求最早到达时间及乘车方案（列明车次与上、下车停站序号），并标出旧表按期"
        "而新表超期或不可达的需求。\n\n"
        "乘车规则：可等待、同车跨站乘坐、最多换车 2 次；首次上车无需缓冲，"
        "换车须满足接续车次发车不早于到达时刻加本站最短换乘秒数，等号可接上。\n\n"
        "**环线示例：** " + RING_EXAMPLE_EXPLANATION + "\n\n"
        "**换乘临界示例：** " + TRANSFER_EXAMPLE_EXPLANATION
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=[
        {"name": "impact", "description": "新增班次影响分析"},
        {"name": "examples", "description": "可调用示例（环线、换乘临界）"},
        {"name": "system", "description": "服务状态"},
    ],
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.get(
    "/example",
    tags=["examples"],
    summary="获取环线与换乘临界两个示例的请求、预期响应和 curl",
)
def example() -> dict:
    return {
        "ring_line_example": {
            "description": RING_EXAMPLE_EXPLANATION,
            "request": RING_EXAMPLE_REQUEST,
            "expected_response": RING_EXAMPLE_RESPONSE,
            "curl": (
                "curl -s -X POST http://localhost:8000/impact "
                "-H 'Content-Type: application/json' "
                f"-d '{json.dumps(RING_EXAMPLE_REQUEST, ensure_ascii=False)}'"
            ),
        },
        "transfer_critical_example": {
            "description": TRANSFER_EXAMPLE_EXPLANATION,
            "request": TRANSFER_EXAMPLE_REQUEST,
            "expected_response": TRANSFER_EXAMPLE_RESPONSE,
            "curl": (
                "curl -s -X POST http://localhost:8000/impact "
                "-H 'Content-Type: application/json' "
                f"-d '{json.dumps(TRANSFER_EXAMPLE_REQUEST, ensure_ascii=False)}'"
            ),
        },
    }


@app.post(
    "/impact",
    tags=["impact"],
    response_model=ImpactResponse,
    summary="分析时刻表调整对每条出行需求的影响",
    description=(
        "分别在旧表、新表上求每条需求的最早到达时间与乘车方案；不可达单列，"
        "并标记旧表按期而新表超期（late）或不可达（unreachable）的需求。"
    ),
    responses={
        200: {
            "description": "成功；results 按需求顺序给出新旧两表的最早到达与方案。",
            "content": {
                "application/json": {
                    "examples": {
                        "ring_line": {
                            "summary": "环线重复停站：两条需求超期",
                            "value": RING_EXAMPLE_RESPONSE,
                        },
                        "transfer_critical": {
                            "summary": "换乘临界：一条需求变为不可达",
                            "value": TRANSFER_EXAMPLE_RESPONSE,
                        },
                    }
                }
            },
        },
        422: {"description": "输入校验失败（结构、时刻顺序、换乘秒数覆盖等）。"},
    },
)
def impact(
    payload: ImpactRequest = Body(
        ...,
        openapi_examples={
            "ring_line": {
                "summary": "环线示例（重复停站、整体延后）",
                "description": RING_EXAMPLE_EXPLANATION,
                "value": RING_EXAMPLE_REQUEST,
            },
            "transfer_critical": {
                "summary": "换乘临界示例（缓冲等号、接续失败）",
                "description": TRANSFER_EXAMPLE_EXPLANATION,
                "value": TRANSFER_EXAMPLE_REQUEST,
            },
        },
    ),
) -> ImpactResponse:
    try:
        return _solve(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# 便于对接的别名，不纳入 /docs，避免与主入口混淆。
@app.post("/analyze", include_in_schema=False, response_model=ImpactResponse)
def analyze_alias(payload: ImpactRequest) -> ImpactResponse:
    return impact(payload)


@app.post("/api/v1/impact", include_in_schema=False, response_model=ImpactResponse)
def impact_v1_alias(payload: ImpactRequest) -> ImpactResponse:
    return impact(payload)
