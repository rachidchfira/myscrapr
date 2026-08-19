from __future__ import annotations

import io
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mapslead import provider as provider_module
from mapslead.errors import ProviderSetupError
from mapslead.models import ProviderCandidate, ProviderRequest
from mapslead.provider import GosomDockerProvider, ProcessOutcome, SubprocessRunner


@dataclass
class RunnerCall:
    args: list[str]
    cwd: Path


@dataclass
class FakeRunner:
    writer: Callable[[Path], None]
    returncode: int = 0
    stdout_tail: str = ""
    stderr_tail: str = ""
    interrupted: bool = False
    calls: list[RunnerCall] = field(default_factory=list)

    def run(self, args: list[str], cwd: Path) -> ProcessOutcome:
        self.calls.append(RunnerCall(args=args, cwd=cwd))
        first_mount_index = args.index("-v")
        second_mount_index = args.index("-v", first_mount_index + 1)
        out_dir = Path(args[second_mount_index + 1].split(":", 1)[0])
        results_path = out_dir / "results.csv"
        self.writer(results_path)
        return ProcessOutcome(
            returncode=self.returncode,
            stdout_tail=self.stdout_tail,
            stderr_tail=self.stderr_tail,
            interrupted=self.interrupted,
        )


@pytest.fixture
def fixture_results_csv() -> Path:
    return Path(__file__).parent / "fixtures" / "provider" / "results.csv"


def copy_fixture(source: Path) -> Callable[[Path], None]:
    def write(results_path: Path) -> None:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, results_path)

    return write


def collect_candidates() -> tuple[list[ProviderCandidate], Callable[[ProviderCandidate], None]]:
    candidates: list[ProviderCandidate] = []

    def sink(candidate: ProviderCandidate) -> None:
        candidates.append(candidate)

    return candidates, sink


def test_acquire_runs_docker_with_expected_args_and_query_file(
    tmp_path: Path,
    fixture_results_csv: Path,
) -> None:
    request = ProviderRequest(
        business="dentists",
        location="Ho Chi Minh City",
        provider_dir=tmp_path / "provider",
        max_new_records=25,
    )
    runner = FakeRunner(writer=copy_fixture(fixture_results_csv))
    provider = GosomDockerProvider(process_runner=runner)
    candidates, sink = collect_candidates()

    result = provider.acquire(request, sink)

    assert result.status == "completed"
    assert result.candidate_count == 2
    assert result.rejected_row_count == 0
    assert len(candidates) == 2
    assert len(runner.calls) == 1

    call = runner.calls[0]
    attempt_dir = request.provider_dir.resolve() / "attempt-0001"
    assert call.cwd == attempt_dir
    assert call.args == [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{(attempt_dir / 'queries.txt').resolve()}:/queries.txt:ro",
        "-v",
        f"{(attempt_dir / 'out').resolve()}:/out",
        "gosom/google-maps-scraper",
        "-input",
        "/queries.txt",
        "-results",
        "/out/results.csv",
        "-depth",
        "1",
        "-c",
        "1",
        "-exit-on-inactivity",
        "3m",
    ]
    assert (attempt_dir / "queries.txt").read_text(encoding="utf-8") == "dentists in Ho Chi Minh City\n"


def test_aliases_map_to_internal_model_and_malformed_rows_are_rejected(
    tmp_path: Path,
) -> None:
    def write(results_path: Path) -> None:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(
            "title,placeId,categories,address,phone,website,totalScore,reviewsCount,url\n"
            "Gamma Dental,ChIJ-gamma,Dentist,3 Main St,+84 28 555 666,https://gamma.example,4.9,31,https://maps.google.com/?cid=gamma\n"
            "Broken Dental,ChIJ-broken,Dentist,4 Main St,+84 28 777 888,https://broken.example,bad,NaN,https://maps.google.com/?cid=broken\n",
            encoding="utf-8",
        )

    request = ProviderRequest(
        business="dentists",
        location="District 1",
        provider_dir=tmp_path / "provider",
        max_new_records=10,
    )
    runner = FakeRunner(writer=write)
    provider = GosomDockerProvider(process_runner=runner)
    candidates, sink = collect_candidates()

    result = provider.acquire(request, sink)

    assert result.status == "completed"
    assert result.candidate_count == 1
    assert result.rejected_row_count == 1
    assert len(candidates) == 1
    assert candidates[0] == ProviderCandidate(
        place_id="ChIJ-gamma",
        name="Gamma Dental",
        category="Dentist",
        address="3 Main St",
        phone="+84 28 555 666",
        website="https://gamma.example",
        rating=4.9,
        review_count=31,
        google_maps_url="https://maps.google.com/?cid=gamma",
    )


@pytest.mark.parametrize(
    ("stdout_tail", "stderr_tail"),
    [
        ("captcha challenge", ""),
        ("", "unusual traffic"),
        ("too many requests", ""),
        ("", "rate limit"),
        ("429", ""),
    ],
)
def test_blocking_diagnostics_return_blocked(
    tmp_path: Path,
    fixture_results_csv: Path,
    stdout_tail: str,
    stderr_tail: str,
) -> None:
    request = ProviderRequest(
        business="dentists",
        location="District 3",
        provider_dir=tmp_path / "provider",
        max_new_records=10,
    )
    runner = FakeRunner(
        writer=copy_fixture(fixture_results_csv),
        returncode=1,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )
    provider = GosomDockerProvider(process_runner=runner)

    result = provider.acquire(request, lambda candidate: None)

    assert result.status == "blocked"


def test_429_digits_inside_map_coordinates_do_not_report_blocked(
    tmp_path: Path,
    fixture_results_csv: Path,
) -> None:
    request = ProviderRequest(
        business="dentists",
        location="Ho Chi Minh City",
        provider_dir=tmp_path / "provider",
        max_new_records=5,
    )
    runner = FakeRunner(
        writer=copy_fixture(fixture_results_csv),
        stdout_tail=(
            '"status":"success","url":"https://www.google.com/maps/place/example/'
            'data=!3d10.7703556!4d106.6842984","message":"job finished"'
        ),
    )
    provider = GosomDockerProvider(process_runner=runner)

    result = provider.acquire(request, lambda candidate: None)

    assert result.status == "completed"


def test_blocked_status_row_returns_blocked(tmp_path: Path) -> None:
    def write(results_path: Path) -> None:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(
            "title,status\nBlocked Listing,blocked\n",
            encoding="utf-8",
        )

    request = ProviderRequest(
        business="dentists",
        location="District 5",
        provider_dir=tmp_path / "provider",
        max_new_records=10,
    )
    runner = FakeRunner(writer=write)
    provider = GosomDockerProvider(process_runner=runner)

    result = provider.acquire(request, lambda candidate: None)

    assert result.status == "blocked"
    assert result.candidate_count == 0


def test_non_zero_exit_without_blocking_returns_failed(tmp_path: Path) -> None:
    request = ProviderRequest(
        business="dentists",
        location="District 7",
        provider_dir=tmp_path / "provider",
        max_new_records=10,
    )
    runner = FakeRunner(
        writer=lambda results_path: results_path.parent.mkdir(parents=True, exist_ok=True),
        returncode=2,
        stderr_tail="docker exited unexpectedly",
    )
    provider = GosomDockerProvider(process_runner=runner)

    result = provider.acquire(request, lambda candidate: None)

    assert result.status == "failed"
    assert result.candidate_count == 0


def test_exit_zero_without_rows_returns_completed(tmp_path: Path) -> None:
    request = ProviderRequest(
        business="dentists",
        location="District 10",
        provider_dir=tmp_path / "provider",
        max_new_records=10,
    )
    runner = FakeRunner(
        writer=lambda results_path: results_path.parent.mkdir(parents=True, exist_ok=True),
    )
    provider = GosomDockerProvider(process_runner=runner)

    result = provider.acquire(request, lambda candidate: None)

    assert result.status == "completed"
    assert result.candidate_count == 0


def test_interrupted_acquire_ingests_partial_csv_and_returns_partial(
    tmp_path: Path,
    fixture_results_csv: Path,
) -> None:
    request = ProviderRequest(
        business="dentists",
        location="District 11",
        provider_dir=tmp_path / "provider",
        max_new_records=10,
    )
    runner = FakeRunner(
        writer=copy_fixture(fixture_results_csv),
        returncode=130,
        interrupted=True,
        stderr_tail="terminated",
    )
    provider = GosomDockerProvider(process_runner=runner)
    candidates, sink = collect_candidates()

    result = provider.acquire(request, sink)

    assert result.status == "partial"
    assert result.interrupted is True
    assert result.candidate_count == 2
    assert len(candidates) == 2


def test_replay_uses_attempt_order_without_running_docker_and_acquire_creates_next_attempt(
    tmp_path: Path,
    fixture_results_csv: Path,
) -> None:
    provider_dir = tmp_path / "provider"
    attempt_two = provider_dir / "attempt-0002" / "out"
    attempt_ten = provider_dir / "attempt-0010" / "out"
    attempt_two.mkdir(parents=True)
    attempt_ten.mkdir(parents=True)
    shutil.copyfile(fixture_results_csv, attempt_two / "results.csv")
    (attempt_ten / "results.csv").write_text(
        "title,place_id\nZeta Dental,ChIJ-zeta\n",
        encoding="utf-8",
    )
    request = ProviderRequest(
        business="dentists",
        location="Go Vap",
        provider_dir=provider_dir,
        max_new_records=10,
    )
    replay_runner = FakeRunner(writer=lambda results_path: None)
    replay_provider = GosomDockerProvider(process_runner=replay_runner)
    replayed: list[ProviderCandidate] = []

    replay_result = replay_provider.replay(request, replayed.append)

    assert replay_result.status == "completed"
    assert [candidate.place_id for candidate in replayed] == [
        "ChIJ-alpha",
        "ChIJ-beta",
        "ChIJ-zeta",
    ]
    assert replay_runner.calls == []

    acquire_runner = FakeRunner(writer=copy_fixture(fixture_results_csv))
    acquire_provider = GosomDockerProvider(process_runner=acquire_runner)

    acquire_result = acquire_provider.acquire(request, lambda candidate: None)

    assert acquire_result.status == "completed"
    assert len(acquire_runner.calls) == 1
    assert (provider_dir / "attempt-0011" / "queries.txt").exists()


def test_subprocess_runner_translates_startup_oserror_to_provider_setup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(provider_module.subprocess, "Popen", fake_popen)

    with pytest.raises(ProviderSetupError, match="docker not found"):
        SubprocessRunner().run(["docker", "run"], tmp_path)


def test_csv_parser_error_is_controlled_and_preserves_completed_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def write(results_path: Path) -> None:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(
            "title,place_id\n"
            "Alpha Dental,ChIJ-alpha\n"
            f"{'X' * 64},ChIJ-too-large\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(provider_module, "MAX_CSV_FIELD_SIZE", 16)

    request = ProviderRequest(
        business="dentists",
        location="District 2",
        provider_dir=tmp_path / "provider",
        max_new_records=10,
    )
    acquire_runner = FakeRunner(writer=write)
    acquire_provider = GosomDockerProvider(process_runner=acquire_runner)
    acquired: list[ProviderCandidate] = []

    acquire_result = acquire_provider.acquire(request, acquired.append)

    assert acquire_result.status == "failed"
    assert acquire_result.candidate_count == 1
    assert acquire_result.rejected_row_count == 1
    assert [candidate.place_id for candidate in acquired] == ["ChIJ-alpha"]
    assert "field larger than field limit" in acquire_result.diagnostics_tail.lower()

    replayed: list[ProviderCandidate] = []
    replay_result = acquire_provider.replay(request, replayed.append)

    assert replay_result.status == "failed"
    assert replay_result.candidate_count == 1
    assert replay_result.rejected_row_count == 1
    assert [candidate.place_id for candidate in replayed] == ["ChIJ-alpha"]
    assert "field larger than field limit" in replay_result.diagnostics_tail.lower()


def test_subprocess_runner_captures_only_bounded_tails_incrementally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeReadable:
        def __init__(self, data: str) -> None:
            self._buffer = io.StringIO(data)

        def read(self, size: int = -1) -> str:
            return self._buffer.read(size)

        def close(self) -> None:
            self._buffer.close()

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = FakeReadable(("out-" * 2_500) + "stdout-final")
            self.stderr = FakeReadable(("err-" * 2_500) + "stderr-final")
            self.returncode = 0

        def communicate(
            self,
            input: str | None = None,
            timeout: float | None = None,
        ) -> tuple[str, str]:
            raise AssertionError("communicate() should not be used for normal capture")

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def terminate(self) -> None:
            raise AssertionError("terminate() should not be called")

        def kill(self) -> None:
            raise AssertionError("kill() should not be called")

    monkeypatch.setattr(provider_module.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    outcome = SubprocessRunner().run(["docker", "run"], tmp_path)

    assert outcome.returncode == 0
    assert outcome.interrupted is False
    assert len(outcome.stdout_tail) == provider_module.DIAGNOSTIC_TAIL_LIMIT
    assert len(outcome.stderr_tail) == provider_module.DIAGNOSTIC_TAIL_LIMIT
    assert outcome.stdout_tail.endswith("stdout-final")
    assert outcome.stderr_tail.endswith("stderr-final")
