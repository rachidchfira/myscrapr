from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mapslead.errors import ExportError, MapsLeadError, QuotaExceededError, RunStateError
from mapslead.models import (
    EnrichmentResult,
    EnrichmentStatus,
    ExportPaths,
    ProgressEvent,
    ProviderCandidate,
    ProviderRequest,
    ProviderResult,
    RunRecord,
    RunSnapshot,
    RunStatus,
)
from mapslead.normalize import build_identity
from mapslead.ports import ExporterPort, MapsProvider, ProgressSink, RepositoryPort, WebsiteEnricher


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run: RunRecord
    provider_result: ProviderResult
    export_paths: ExportPaths | None
    service_error: str | None = None


class RequestedLimitError(MapsLeadError):
    """Raised when the requested run limit is invalid for the current quota."""


class ResumeNotAllowedError(RunStateError):
    """Raised when a run cannot be resumed from its current state."""


@dataclass(slots=True)
class _AcquisitionState:
    known_identity_keys: set[str]
    candidate_count: int
    new_unique_count: int
    quota_exhausted: bool = False


class MapsLeadService:
    def __init__(
        self,
        repository: RepositoryPort,
        provider: MapsProvider,
        enricher: WebsiteEnricher,
        exporter: ExporterPort,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._enricher = enricher
        self._exporter = exporter

    def scrape(
        self,
        business: str,
        location: str,
        limit: int,
        now: datetime,
        progress: ProgressSink,
    ) -> RunOutcome:
        remaining_quota = self._repository.remaining_quota(now)
        self._validate_requested_limit(limit, remaining_quota)

        run = self._repository.create_run(business, location, limit, now)
        acquisition_state = self._acquisition_state_for_run(run.id)
        request = ProviderRequest(
            business=business,
            location=location,
            provider_dir=run.provider_dir,
            max_new_records=limit,
        )
        provider_result = self._provider.acquire(
            request,
            self._candidate_sink(
                run_id=run.id,
                now=now,
                max_new_unique=limit,
                state=acquisition_state,
                progress=progress,
            ),
        )
        return self._complete_run(run.id, now, provider_result, progress)

    def resume(self, run_id: str, now: datetime, progress: ProgressSink) -> RunOutcome:
        run = self._repository.get_run(run_id)
        if run.status not in {RunStatus.PARTIAL, RunStatus.BLOCKED, RunStatus.FAILED}:
            raise ResumeNotAllowedError(
                f"run {run_id} cannot be resumed from status {run.status.value}"
            )

        acquisition_state = self._acquisition_state_for_run(run.id)
        replay_request = ProviderRequest(
            business=run.business_type,
            location=run.location_query,
            provider_dir=run.provider_dir,
            max_new_records=run.requested_limit,
        )
        provider_result = self._provider.replay(
            replay_request,
            self._candidate_sink(
                run_id=run.id,
                now=now,
                max_new_unique=run.requested_limit,
                state=acquisition_state,
                progress=progress,
            ),
        )

        remaining_capacity = max(0, run.requested_limit - self._repository.new_unique_count_for_run(run.id))
        remaining_quota = self._repository.remaining_quota(now)
        acquire_capacity = min(remaining_capacity, remaining_quota)
        if acquire_capacity > 0:
            acquire_request = ProviderRequest(
                business=run.business_type,
                location=run.location_query,
                provider_dir=run.provider_dir,
                max_new_records=acquire_capacity,
            )
            provider_result = self._provider.acquire(
                acquire_request,
                self._candidate_sink(
                    run_id=run.id,
                    now=now,
                    max_new_unique=self._repository.new_unique_count_for_run(run.id)
                    + acquire_capacity,
                    state=acquisition_state,
                    progress=progress,
                ),
            )

        return self._complete_run(run.id, now, provider_result, progress)

    def _complete_run(
        self,
        run_id: str,
        now: datetime,
        provider_result: ProviderResult,
        progress: ProgressSink,
    ) -> RunOutcome:
        self._run_pending_enrichment(run_id, now, progress)
        updated_run = self._repository.set_run_status(
            run_id,
            self._final_status(provider_result),
            finished_at=now,
            error=self._provider_error(provider_result),
        )

        try:
            export_paths = self._exporter.export_run(run_id)
        except ExportError as error:
            return RunOutcome(
                run=updated_run,
                provider_result=provider_result,
                export_paths=None,
                service_error=str(error),
            )

        progress(
            ProgressEvent(
                kind="export",
                message="Exported run results.",
                run_id=run_id,
                export_paths=export_paths,
            )
        )
        return RunOutcome(
            run=updated_run,
            provider_result=provider_result,
            export_paths=export_paths,
            service_error=None,
        )

    def _candidate_sink(
        self,
        *,
        run_id: str,
        now: datetime,
        max_new_unique: int,
        state: _AcquisitionState,
        progress: ProgressSink,
    ) -> _RepositoryCandidateSink:
        return _RepositoryCandidateSink(
            repository=self._repository,
            run_id=run_id,
            now=now,
            max_new_unique=max_new_unique,
            state=state,
            progress=progress,
        )

    def _acquisition_state_for_run(self, run_id: str) -> _AcquisitionState:
        known_identity_keys: set[str] = set()
        for snapshot in self._repository.snapshots_for_run(run_id):
            known_identity_keys.update(_identity_keys_for_snapshot(snapshot))
        return _AcquisitionState(
            known_identity_keys=known_identity_keys,
            candidate_count=0,
            new_unique_count=self._repository.new_unique_count_for_run(run_id),
        )

    def _run_pending_enrichment(self, run_id: str, now: datetime, progress: ProgressSink) -> None:
        pending = tuple(self._repository.pending_enrichment(run_id))
        total = len(pending)
        for index, snapshot in enumerate(pending, start=1):
            result = self._enrich_snapshot(snapshot)
            self._repository.save_enrichment(run_id, snapshot.business_id, result, now)
            progress(
                ProgressEvent(
                    kind="enrichment",
                    message="Saved enrichment checkpoint.",
                    run_id=run_id,
                    completed_count=index,
                    total_count=total,
                )
            )

    def _enrich_snapshot(self, snapshot: RunSnapshot) -> EnrichmentResult:
        website = snapshot.website
        if website is None:
            return EnrichmentResult(status=EnrichmentStatus.PENDING)
        try:
            return self._enricher.enrich(website)
        except Exception as error:  # noqa: BLE001
            return EnrichmentResult(
                status=EnrichmentStatus.FAILED,
                error=str(error),
            )

    def _validate_requested_limit(self, limit: int, remaining_quota: int) -> None:
        if 1 <= limit <= remaining_quota:
            return
        raise RequestedLimitError(
            f"requested limit {limit} must be between 1 and remaining daily quota {remaining_quota}"
        )

    def _final_status(self, provider_result: ProviderResult) -> RunStatus:
        if provider_result.interrupted:
            return RunStatus.PARTIAL
        if provider_result.status == RunStatus.PARTIAL.value:
            return RunStatus.PARTIAL
        if provider_result.status == RunStatus.BLOCKED.value:
            return RunStatus.BLOCKED
        if provider_result.status == RunStatus.FAILED.value:
            return RunStatus.FAILED
        return RunStatus.COMPLETED

    def _provider_error(self, provider_result: ProviderResult) -> str | None:
        if self._final_status(provider_result) is RunStatus.COMPLETED:
            return None
        diagnostics = provider_result.diagnostics_tail.strip()
        return diagnostics or provider_result.status


def _identity_keys_for_candidate(candidate: ProviderCandidate) -> set[str]:
    try:
        identity = build_identity(candidate)
    except ValueError:
        return set()
    return {identity.primary_key, *identity.aliases}


def _identity_keys_for_snapshot(snapshot: RunSnapshot) -> set[str]:
    candidate = ProviderCandidate(
        place_id=snapshot.place_id,
        name=snapshot.name,
        category=snapshot.category,
        address=snapshot.address,
        phone=snapshot.phone,
        website=snapshot.website,
        rating=snapshot.rating,
        review_count=snapshot.review_count,
        google_maps_url=snapshot.google_maps_url,
    )
    return _identity_keys_for_candidate(candidate)


@dataclass(slots=True)
class _RepositoryCandidateSink:
    repository: RepositoryPort
    run_id: str
    now: datetime
    max_new_unique: int
    state: _AcquisitionState
    progress: ProgressSink

    def __call__(self, candidate: ProviderCandidate) -> None:
        candidate_keys = _identity_keys_for_candidate(candidate)
        is_known_duplicate = bool(candidate_keys & self.state.known_identity_keys)
        if self.state.quota_exhausted and not is_known_duplicate:
            return
        if self.state.new_unique_count >= self.max_new_unique and not (
            candidate_keys & self.state.known_identity_keys
        ):
            return

        try:
            acceptance = self.repository.accept_candidate(self.run_id, candidate, self.now)
        except QuotaExceededError:
            if is_known_duplicate:
                raise
            self.state.quota_exhausted = True
            return
        self.state.candidate_count += 1
        if acceptance.is_new:
            self.state.new_unique_count += 1
        self.state.known_identity_keys.update(candidate_keys)
        self.progress(
            ProgressEvent(
                kind="acquisition",
                message="Accepted acquisition candidate.",
                run_id=self.run_id,
                candidate_count=self.state.candidate_count,
                new_unique_count=self.state.new_unique_count,
            )
        )
