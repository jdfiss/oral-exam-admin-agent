"""統一的口試業務流程 interface。"""

from __future__ import annotations

from dataclasses import dataclass
import os
import tempfile
from pathlib import Path

from . import config as C
from . import ai_proposals
from . import document_conversion
from . import ingest
from . import master as master_workbook
from . import source_state
from . import validation
from . import util as U
from . import committee_requests
from . import corrections
from .generators import attachment4 as attachment4_generator
from .generators import attachment6 as attachment6_generator
from .model import Master
from .registry import CommitteeRegistry, DistanceTable


FATAL_INGEST_CODES = {
    # 只保留「無法確定合併」的情況；附件2 的資料問題一律逐案列為例外交專員，
    # 不讓一筆 Portal 資料擋住截止日當天的整批建檔（review-2026-09-23 P0-3）。
    "DUPLICATE_SUBMISSION",
    "DUPLICATE_COMMITTEE_IDENTITY",
    # 歷史附件3匯入（過渡期保留，主流程不觸發）
    "ATTACHMENT_3_PARSE_ERROR",
    "ATTACHMENT_3_WITHOUT_APPLICATION",
    "DUPLICATE_ATTACHMENT_3",
}


@dataclass(frozen=True)
class BuildResult:
    master: Master
    issues: list[dict[str, object]]
    output_path: Path


@dataclass(frozen=True)
class TemplatePaths:
    attachment4: Path
    attachment6: Path


@dataclass(frozen=True)
class RegistryPaths:
    members: Path
    distances: Path
    members_output: Path | None = None
    attachment2: Path | None = None  # 系辦上傳的 Portal 匯出；呼叫端未指定 attachment2_path 時使用
    committee_replies: Path | None = None  # 委員回填的撥帳資料（state/committee_replies.json）
    corrections: Path | None = None  # 專員修正層（state/corrections.json，2026-10-01）


@dataclass(frozen=True)
class GenerationResult:
    attachment4: Path
    attachment6_files: list[Path]
    issues: list[dict[str, object]]


@dataclass(frozen=True)
class PreExamResult:
    attachment4: Path
    attachment6_files: list[Path]
    issues: list[dict[str, object]]
    manifest: Path | None = None


@dataclass(frozen=True)
class PostExamResult:
    attachment6_files: list[Path]
    issues: list[dict[str, object]]
    manifest: Path | None = None


def build_master(
    *, attachment2_path: str | Path | None = None, submission_paths: list[str | Path],
    output_path: str | Path, registries: RegistryPaths | None = None,
    submissions_dir: str | Path | None = None,
) -> BuildResult:
    """包含直接CLI建檔在內，對同一主檔的整輪讀取與發布串行執行。"""
    with U.state_lock(Path(output_path).parent):
        return _build_master(attachment2_path=attachment2_path, submission_paths=submission_paths,
            output_path=output_path, registries=registries, submissions_dir=submissions_dir)


def _build_master(
    *,
    attachment2_path: str | Path | None = None,
    submission_paths: list[str | Path],
    output_path: str | Path,
    registries: RegistryPaths | None = None,
    submissions_dir: str | Path | None = None,
) -> BuildResult:
    """讀取附件2與學生填寫，建立並保存唯一 canonical Master。

    案件只來自附件2（Portal 匯出，可提早上傳、有誤可整份重傳）；沒有附件2 時
    主檔沒有任何案件。學生填寫只補附件 3 資料（2026-09-23 定案）。委員先
    建立案件級 P... 暫定身分，強識別後才升級或合併為 CommitteeRegistry
    跨批次持久化的 C... 正式身分與撥帳資料。
    """
    output = Path(output_path)
    registries = registries or RegistryPaths(
        members=C.REG_MEMBER,
        distances=C.DISTANCE_CSV,
        members_output=C.CURRENT_DIR / "data" / "委員個資.xlsx",
    )
    registry_output = registries.members_output or output.parent / "data" / "委員個資.xlsx"
    source_paths = [registry_output, registries.members, registries.distances, attachment2_path or registries.attachment2,
                    registries.committee_replies, registries.corrections, *submission_paths]
    source_dirs = {Path(p).parent for p in submission_paths}
    if submissions_dir is not None:
        source_dirs.add(Path(submissions_dir))
        submission_paths = sorted({Path(p) for p in submission_paths} | set(Path(submissions_dir).glob("*.json")))
        source_paths = [registry_output, registries.members, registries.distances,
            attachment2_path or registries.attachment2, registries.committee_replies,
            registries.corrections, *submission_paths]
    source_dirs = sorted(source_dirs)
    # 專員確認的服務單位對照表與修正層同放狀態目錄；對照一變就算來源變更、要重建。
    alias_path = (Path(registries.corrections).parent / ai_proposals.ALIASES_FILE
                  if registries.corrections is not None else None)
    source_paths.append(alias_path)
    source_fingerprint = source_state.fingerprint(source_paths, source_dirs)
    committee_registry = CommitteeRegistry.load(registry_output if registry_output.is_file() else registries.members)
    if registry_output.is_file():
        committee_registry.import_source(CommitteeRegistry.load(registries.members))
    distance_table = DistanceTable.load(registries.distances)
    if alias_path is not None:
        distance_table.aliases = ai_proposals.alias_map(alias_path.parent)

    attachment2_path = attachment2_path or registries.attachment2
    if attachment2_path is not None and Path(attachment2_path).is_file():
        exams, issues = ingest.read_att2(committee_registry, attachment2_path)
    else:
        exams, issues = {}, []
    issues.extend(ingest.read_student_submissions(exams, committee_registry, submission_paths))
    for exam in exams.values():
        for member in exam.委員:
            profile = committee_registry.get(member.committee_id) or {}
            member.association_id = committee_registry.new_provisional_id(
                exam.案件鍵, profile.get("委員姓名", ""), member.服務單位)
            member.委員姓名 = str(profile.get("委員姓名") or "")
            member.Email = str(profile.get("Email") or "")
    fatal_codes = sorted(
        {
            str(item.get("問題代碼"))
            for item in issues
            if item.get("問題代碼") in FATAL_INGEST_CODES
        }
    )
    if fatal_codes:
        raise ValueError(
            "輸入資料無法確定合併，未寫入任何主檔或委員資料："
            + "、".join(fatal_codes)
        )

    committees = ingest.materialize_committees(committee_registry, exams)
    if registries.corrections is not None:
        # 專員修正在來源之後、推導與檢核之前套用：重傳附件2／學生重送都不會蓋掉
        correction_dir = Path(registries.corrections).parent
        conflicts = corrections.apply(exams, committees, corrections.load(correction_dir), registry=committee_registry)
        corrections.save_conflicts(correction_dir, conflicts)
        issues.extend(conflicts)
        # 從未送出、但專員已以修正層補齊必要資料的案件：「尚未填寫」視為解決（決策三-5）
        staff_filled = corrections.mark_staff_filled(exams, committees, corrections.load(correction_dir))
        issues = [i for i in issues
                  if not (i.get("問題代碼") == "MISSING_SUBMISSION" and i.get("案件鍵") in staff_filled)]
        for exam in exams.values():
            for member in exam.委員:
                profile = committee_registry.get(member.committee_id) or {}
                if member.委員姓名 and U.canon_name(member.委員姓名) != U.canon_name(profile.get("委員姓名")):
                    member.committee_id = committee_registry.new_provisional_id(
                        exam.案件鍵, member.委員姓名, member.服務單位, source="專員姓名修正")
    if registries.committee_replies is not None:
        issues.extend(ingest.apply_committee_replies(
            exams, committee_registry, committee_requests.read_replies_file(registries.committee_replies)))
    issues.extend(ingest.reuse_registry_by_name_unit(exams, committee_registry))
    committees = ingest.materialize_committees(committee_registry, exams)
    ingest.fill_advisors(exams, committees)
    ingest.fill_fees(exams, committees, distance_table)

    for exam in exams.values():
        exam.derive()

    canonical = Master(
        exams=sorted(exams.values(), key=lambda exam: (exam.學期, exam.學號)),
        committees=committees, source_issues=list(issues),
    )
    issues = _deduplicate_issues([*issues, *validation.check(canonical)])
    # resource 下的來源檔一律只讀；合併附件3後的 Registry 與主檔同批輸出。
    registry_output = registries.members_output or output.parent / "data" / "委員個資.xlsx"
    output.parent.mkdir(parents=True, exist_ok=True)
    registry_output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".build-") as stage:
        staged_master, staged_registry = Path(stage) / "master.xlsx", Path(stage) / "registry.xlsx"
        committee_registry.save(staged_registry)
        master_workbook.save(canonical, issues, path=staged_master)
        if source_state.fingerprint(source_paths, source_dirs) != source_fingerprint:
            raise ValueError("建檔期間來源已變更，本輪結果未發布；請重新建檔")
        os.replace(staged_registry, registry_output)
        os.replace(staged_master, output)
        source_fingerprint[str(registry_output.resolve())] = source_state.digest(registry_output)
        source_state.publish(output, source_fingerprint, source_dirs)
        source_state.bind(canonical, output)

    # 距離表是外部權威資料，只讀、不在建檔流程中產生待填檔。
    return BuildResult(master=canonical, issues=issues, output_path=output)


def _deduplicate_issues(issues: list[dict[str, object]]) -> list[dict[str, object]]:
    unique: dict[tuple[object, object, object], dict[str, object]] = {}
    case_level_codes = {"MISSING_SUBMISSION", "CASE_CANCELLED"}
    for item in issues:
        code = item.get("問題代碼")
        field = None if code in case_level_codes else item.get("欄位")
        key = (item.get("案件鍵"), code, field)
        unique.setdefault(key, item)
    return list(unique.values())


def generate_456(
    master: Master,
    *,
    templates: TemplatePaths,
    output_dir: str | Path,
    term: str | None = None,
) -> GenerationResult:
    """從 canonical Master 產生附件4／6（附件5 已於 2026-09-23 取消）。"""
    output = Path(output_dir)
    expected_templates = (
        ("附件4", Path(templates.attachment4), {".doc", ".docx"}, ".doc 或 .docx"),
        ("附件6", Path(templates.attachment6), {".xlsx"}, ".xlsx"),
    )
    invalid_templates = [
        f"{name}={path}（需要 {label}）"
        for name, path, extensions, label in expected_templates
        if not path.is_file() or path.suffix.lower() not in extensions
    ]
    if invalid_templates:
        raise FileNotFoundError(
            "附件範本不完整，未產生任何檔案：" + "；".join(invalid_templates)
        )

    issues = validation.check(master)
    blocked = validation.blocked_keys(issues)
    allowed = Master(
        exams=[exam for exam in master.exams if exam.案件鍵 not in blocked],
        committees=master.committees,
    )

    attachment4 = output / "附件4-學位口試一覽.docx"
    # 官方附件4只有舊式 .doc；先在隔離暫存目錄轉成 .docx。
    # 轉檔若失敗會在任何正式輸出建立前停止。
    with document_conversion.docx_template(templates.attachment4) as attachment4_template:
        attachment4_generator.generate(
            allowed, attachment4, template=attachment4_template, term=term
        )
        summaries = attachment6_generator.generate(
            allowed, output / "附件6", template=templates.attachment6, term=term
        )
    return GenerationResult(
        attachment4=attachment4,
        attachment6_files=[Path(summary["輸出"]) for summary in summaries],
        issues=issues,
    )


def _validate_templates(templates, needed):
    invalid = [
        f"{name}={path}（需要 {label}）"
        for name, path, extensions, label in needed
        if not path.is_file() or path.suffix.lower() not in extensions
    ]
    if invalid:
        raise FileNotFoundError(
            "附件範本不完整，未產生任何檔案：" + "；".join(invalid)
        )


def generate_pre_exam(
    master: Master,
    *,
    templates: TemplatePaths,
    output_dir: str | Path,
    term: str | None = None,
) -> PreExamResult:
    """口試前：只產附件4 + 預填附件6（銀行欄留白）。"""
    output = Path(output_dir)
    _validate_templates(templates, (
        ("附件4", Path(templates.attachment4), {".doc", ".docx"}, ".doc 或 .docx"),
        ("附件6", Path(templates.attachment6), {".xlsx"}, ".xlsx"),
    ))

    issues = validation.check(master)
    blocked = validation.attachment4_blocked_keys(issues)
    allowed = Master(
        exams=[exam for exam in master.exams if exam.案件鍵 not in blocked],
        committees=master.committees,
    )

    attachment4 = output / "附件4-學位口試一覽.docx"
    with document_conversion.docx_template(templates.attachment4) as tpl:
        attachment4_generator.generate(allowed, attachment4, template=tpl, term=term)
    # 交付版個資四欄留白、委員可在口試現場補齊：未交個資／身分暫定不擋（決策五-4、七-1）
    fee_blocked = validation.attachment6_prefill_blocked_keys(issues)
    fee_allowed = Master(exams=[e for e in allowed.exams if e.案件鍵 not in fee_blocked],
                         committees=allowed.committees)
    summaries = attachment6_generator.generate(
        fee_allowed, output / "附件6-預填", template=templates.attachment6,
        term=term, prefill=True,
    )
    return PreExamResult(
        attachment4=attachment4,
        attachment6_files=[Path(s["輸出"]) for s in summaries],
        issues=issues,
    )


def generate_post_exam(
    master: Master,
    *,
    templates: TemplatePaths,
    output_dir: str | Path,
    term: str | None = None,
) -> PostExamResult:
    """口試後：產完整附件6（含銀行資訊）。附件5 已取消。"""
    output = Path(output_dir)
    _validate_templates(templates, (
        ("附件6", Path(templates.attachment6), {".xlsx"}, ".xlsx"),
    ))

    issues = validation.check(master)
    blocked = validation.attachment6_blocked_keys(issues)
    allowed = Master(
        exams=[exam for exam in master.exams if exam.案件鍵 not in blocked],
        committees=master.committees,
    )

    summaries = attachment6_generator.generate(
        allowed, output / "附件6", template=templates.attachment6, term=term,
    )
    return PostExamResult(
        attachment6_files=[Path(s["輸出"]) for s in summaries],
        issues=issues,
    )
