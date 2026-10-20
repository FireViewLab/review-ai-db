"""격리 벤치 도구의 운영 대상 차단과 병렬 처리 판정 기준을 검증한다."""

from argparse import Namespace
from unittest.mock import Mock

import pytest

from scripts import benchmark_batch_workers as benchmark
from scripts.benchmark_worker_app import require_disposable_environment


@pytest.mark.parametrize("base_url,acknowledged", [
    ("http://example.com", True), ("https://127.0.0.1", True),
    ("http://127.0.0.1", False), ("http://user:password@127.0.0.1", True),
])
def test_benchmark_rejects_non_disposable_targets_before_docker(monkeypatch, base_url, acknowledged):
    forbidden = Mock(side_effect=AssertionError("Target must be rejected before Docker access"))
    monkeypatch.setattr(benchmark, "docker", forbidden)
    with pytest.raises(ValueError):
        benchmark.verify_target(Namespace(base_url=base_url, allow_disposable=acknowledged,
                                          container_name=benchmark.EXPECTED_CONTAINER))
    forbidden.assert_not_called()


def test_worker_wrapper_requires_disposable_db_and_groq_off(monkeypatch):
    monkeypatch.setenv("BENCHMARK_DISPOSABLE", "1")
    monkeypatch.setenv("DB_HOST", "ai-db")
    monkeypatch.setenv("DB_NAME", "runtime_validation")
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "0")
    require_disposable_environment()
    monkeypatch.setenv("DB_NAME", "review_system")
    with pytest.raises(RuntimeError):
        require_disposable_environment()
    monkeypatch.setenv("DB_NAME", "runtime_validation")
    monkeypatch.setenv("ENABLE_GROQ_REASON_NATURALIZATION", "1")
    with pytest.raises(RuntimeError):
        require_disposable_environment()


def test_overlap_requires_distinct_processes_and_real_interval_intersection():
    def events(pid, identity, start, end):
        return [dict(kind="analysis_start", pid=pid, analysis_id=identity,
                     product_id=identity, monotonic=start),
                dict(kind="analysis_end", pid=pid, analysis_id=identity,
                     product_id=identity, monotonic=end, succeeded=True)]

    assert benchmark.overlap_pairs(events(1, "a", 0, 4) + events(1, "b", 2, 3)) == []
    assert benchmark.overlap_pairs(events(1, "a", 0, 2) + events(2, "b", 2, 4)) == []
    pairs = benchmark.overlap_pairs(events(1, "a", 0, 4) + events(2, "b", 2, 5))
    assert pairs == [dict(pids=[1, 2], seconds=2, products=["a", "b"])]


def test_docker_memory_units_are_normalized_without_treating_limit_as_usage():
    assert benchmark.memory_bytes("512MiB / 4GiB") == 512 * 1024 ** 2
    assert benchmark.memory_bytes("1.25GB / 4GB") == 1250000000


def test_observation_parser_accepts_joined_records_and_unrelated_log_lines():
    raw = ('startup\nBENCHMARK_EVENT {"kind":"analysis_start","pid":1}'
           'BENCHMARK_EVENT {"kind":"analysis_end","pid":1}\n'
           'INFO: unrelated log line\nBENCHMARK_EVENT {"kind":"model_load_end","pid":2}\n')
    assert benchmark.parse_observed_events(raw) == [
        dict(kind="analysis_start", pid=1), dict(kind="analysis_end", pid=1),
        dict(kind="model_load_end", pid=2),
    ]
