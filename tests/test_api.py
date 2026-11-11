"""新增班次影响分析 API 的接口测试。"""

import unittest

try:
    from fastapi.testclient import TestClient

    from app.main import (
        RING_EXAMPLE_REQUEST,
        TRANSFER_EXAMPLE_REQUEST,
        app,
    )
except ImportError:  # 允许在仅含求解器的环境中跳过接口测试
    TestClient = None
    app = None
    RING_EXAMPLE_REQUEST = None
    TRANSFER_EXAMPLE_REQUEST = None


@unittest.skipIf(TestClient is None, "未安装 FastAPI 测试依赖")
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_health_docs_and_openapi(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/docs").status_code, 200)
        self.assertEqual(self.client.get("/redoc").status_code, 200)
        openapi = self.client.get("/openapi.json").json()
        self.assertIn("/impact", openapi["paths"])
        examples = (
            openapi["paths"]["/impact"]["post"]["requestBody"]
            ["content"]["application/json"]["examples"]
        )
        self.assertEqual(set(examples), {"ring_line", "transfer_critical"})

    def test_root_redirects_to_docs(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/docs")

    def test_ring_line_example(self):
        response = self.client.post("/impact", json=RING_EXAMPLE_REQUEST)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        by_id = {item["demand_id"]: item for item in body["results"]}

        r1 = by_id["R1"]
        self.assertTrue(r1["affected"])
        self.assertEqual(r1["impact_type"], "late")
        self.assertEqual(r1["old"]["earliest_arrival"], 100)
        self.assertTrue(r1["old"]["on_time"])
        self.assertEqual(r1["new"]["earliest_arrival"], 220)
        self.assertFalse(r1["new"]["on_time"])
        # 环线重复停站：方案列明车次与停站序号。
        old_leg = r1["old"]["journey"]["legs"][0]
        self.assertEqual(old_leg["trip_id"], "环线L1")
        self.assertEqual(
            (old_leg["board_stop_index"], old_leg["alight_stop_index"]), (1, 2)
        )

        r2 = by_id["R2"]
        self.assertTrue(r2["affected"])
        self.assertEqual(r2["impact_type"], "late")
        # B -> A 乘至第二圈 A 站，即第 2 停站上车、第 5 停站下车。
        leg = r2["old"]["journey"]["legs"][0]
        self.assertEqual(
            (leg["board_stop_index"], leg["alight_stop_index"]), (2, 5)
        )
        self.assertEqual(r2["old"]["earliest_arrival"], 400)
        self.assertEqual(r2["new"]["earliest_arrival"], 520)

        r3 = by_id["R3"]
        self.assertFalse(r3["affected"])
        self.assertIsNone(r3["impact_type"])
        self.assertEqual(r3["new"]["earliest_arrival"], 420)
        self.assertTrue(r3["new"]["on_time"])

        summary = body["summary"]
        self.assertEqual(summary["total_demands"], 3)
        self.assertEqual(summary["affected_count"], 2)
        self.assertEqual(summary["affected_late_count"], 2)
        self.assertEqual(summary["affected_unreachable_count"], 0)
        self.assertEqual(summary["new_on_time_count"], 1)

    def test_transfer_critical_example(self):
        response = self.client.post("/impact", json=TRANSFER_EXAMPLE_REQUEST)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        by_id = {item["demand_id"]: item for item in body["results"]}

        m1 = by_id["M1"]
        self.assertTrue(m1["affected"])
        self.assertEqual(m1["impact_type"], "unreachable")
        self.assertTrue(m1["old"]["reachable"])
        self.assertEqual(m1["old"]["earliest_arrival"], 500)
        self.assertEqual(m1["old"]["journey"]["transfer_count"], 1)
        self.assertEqual(
            [leg["trip_id"] for leg in m1["old"]["journey"]["legs"]], ["T1", "T2"]
        )
        # 第二次上车使用了 P 站 60 秒换乘缓冲。
        self.assertEqual(
            m1["old"]["journey"]["legs"][1]["transfer_buffer_seconds"], 60
        )
        self.assertFalse(m1["new"]["reachable"])
        self.assertIsNone(m1["new"]["earliest_arrival"])
        self.assertIsNone(m1["new"]["journey"])

        m2 = by_id["M2"]
        self.assertFalse(m2["affected"])
        self.assertEqual(m2["old"]["earliest_arrival"], 600)
        self.assertEqual(m2["new"]["earliest_arrival"], 600)
        self.assertEqual(m2["old"]["journey"]["transfer_count"], 0)

        summary = body["summary"]
        self.assertEqual(summary["affected_count"], 1)
        self.assertEqual(summary["affected_unreachable_count"], 1)
        self.assertEqual(summary["new_unreachable_count"], 1)

    def test_example_endpoint_and_aliases(self):
        example = self.client.get("/example")
        self.assertEqual(example.status_code, 200)
        payload = example.json()["transfer_critical_example"]["request"]
        for path in ("/analyze", "/api/v1/impact"):
            response = self.client.post(path, json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["results"][0]["impact_type"], "unreachable"
            )

    def test_origin_equals_destination(self):
        payload = {
            "old_timetable": {
                "label": "旧",
                "trips": [
                    {
                        "trip_id": "T1",
                        "stops": [
                            {"station": "A", "arrival": 0, "departure": 10},
                            {"station": "B", "arrival": 20, "departure": 20},
                        ],
                    }
                ],
            },
            "new_timetable": {
                "label": "新",
                "trips": [
                    {
                        "trip_id": "T1",
                        "stops": [
                            {"station": "A", "arrival": 0, "departure": 10},
                            {"station": "B", "arrival": 20, "departure": 20},
                        ],
                    }
                ],
            },
            "minimum_transfer_seconds": {"A": 0, "B": 0},
            "demands": [
                {
                    "demand_id": "D1",
                    "origin": "A",
                    "destination": "A",
                    "departure_time": 55,
                    "arrival_deadline": 100,
                }
            ],
        }
        body = self.client.post("/impact", json=payload).json()
        result = body["results"][0]
        self.assertEqual(result["old"]["earliest_arrival"], 55)
        self.assertEqual(result["old"]["journey"]["legs"], [])
        self.assertEqual(result["old"]["journey"]["transfer_count"], 0)
        self.assertFalse(result["affected"])

    def test_validation_errors(self):
        # 停站时刻顺序非法。
        bad_time = {
            "old_timetable": {
                "label": "旧",
                "trips": [
                    {
                        "trip_id": "T1",
                        "stops": [
                            {"station": "A", "arrival": 100, "departure": 90},
                            {"station": "B", "arrival": 200, "departure": 200},
                        ],
                    }
                ],
            },
            "new_timetable": {
                "label": "新",
                "trips": [
                    {
                        "trip_id": "T1",
                        "stops": [
                            {"station": "A", "arrival": 0, "departure": 0},
                            {"station": "B", "arrival": 10, "departure": 10},
                        ],
                    }
                ],
            },
            "minimum_transfer_seconds": {"A": 0, "B": 0},
            "demands": [
                {
                    "demand_id": "D1",
                    "origin": "A",
                    "destination": "B",
                    "departure_time": 0,
                    "arrival_deadline": 500,
                }
            ],
        }
        self.assertEqual(
            self.client.post("/impact", json=bad_time).status_code, 422
        )

        # 缺少换乘秒数。
        missing_transfer = {
            "old_timetable": {
                "label": "旧",
                "trips": [
                    {
                        "trip_id": "T1",
                        "stops": [
                            {"station": "A", "arrival": 0, "departure": 0},
                            {"station": "B", "arrival": 10, "departure": 10},
                        ],
                    }
                ],
            },
            "new_timetable": {
                "label": "新",
                "trips": [
                    {
                        "trip_id": "T1",
                        "stops": [
                            {"station": "A", "arrival": 0, "departure": 0},
                            {"station": "B", "arrival": 10, "departure": 10},
                        ],
                    }
                ],
            },
            "minimum_transfer_seconds": {"A": 0},
            "demands": [
                {
                    "demand_id": "D1",
                    "origin": "A",
                    "destination": "B",
                    "departure_time": 0,
                    "arrival_deadline": 500,
                }
            ],
        }
        self.assertEqual(
            self.client.post("/impact", json=missing_transfer).status_code, 422
        )

        # 多余字段被拒绝。
        extra_field = dict(RING_EXAMPLE_REQUEST)
        extra_field["unexpected"] = 1
        self.assertEqual(
            self.client.post("/impact", json=extra_field).status_code, 422
        )

    def test_demand_limit_enforced(self):
        base = RING_EXAMPLE_REQUEST
        too_many = dict(base)
        too_many["demands"] = [
            {
                "demand_id": f"D{i}",
                "origin": "A站",
                "destination": "B站",
                "departure_time": 0,
                "arrival_deadline": 10000,
            }
            for i in range(31)
        ]
        self.assertEqual(
            self.client.post("/impact", json=too_many).status_code, 422
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
