# -*- coding: utf-8 -*-
"""責任分類器 —— 判定每個缺件由學生或系辦負責補正。

薄包裝：實際對照表在 validation.ISSUE_OWNER，這裡提供便利的分類 API。
"""

from __future__ import annotations

from .validation import ISSUE_OWNER, OWNER_STUDENT, OWNER_OFFICE, OWNER_POST_EXAM, OWNER_NONE

STUDENT = OWNER_STUDENT
DEPARTMENT = OWNER_OFFICE
POST_EXAM = OWNER_POST_EXAM
NO_NOTIFY = OWNER_NONE


def classify(issue: dict) -> str:
    """回傳責任方。"""
    code = str(issue.get("問題代碼") or "")
    return ISSUE_OWNER.get(code, DEPARTMENT)


def partition(issues: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """把 issues 分成 (student_issues, department_issues, post_exam_issues)。

    OWNER_NONE（資訊性）不進任何桶。
    """
    student, department, post_exam = [], [], []
    for issue in issues:
        owner = classify(issue)
        if owner == STUDENT:
            student.append(issue)
        elif owner == DEPARTMENT:
            department.append(issue)
        elif owner == POST_EXAM:
            post_exam.append(issue)
    return student, department, post_exam
