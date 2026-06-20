"""Structured output schemas for all agents — validated with Pydantic."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Task(BaseModel):
    description: str
    file_path: str
    details: str = ""


class Plan(BaseModel):
    problem_description: str
    files_needed: list[str] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


class CodeFile(BaseModel):
    path: str
    content: str


class CodeBundle(BaseModel):
    files: list[CodeFile] = Field(default_factory=list)
    explanation: str = ""


class TestResult(BaseModel):
    passed: bool
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    test_summary: str = ""
    duration_seconds: float = 0.0


class ReviewComment(BaseModel):
    file_path: str = ""
    line: int | None = None
    severity: str = "info"
    message: str = ""


class Review(BaseModel):
    approved: bool
    comments: list[ReviewComment] = Field(default_factory=list)
    summary: str = ""


class AttemptRecord(BaseModel):
    iteration: int
    code_bundle: CodeBundle | None = None
    test_result: TestResult | None = None
    review: Review | None = None
    error_hash: str = ""


class ProductSpec(BaseModel):
    milestones: list[str] = Field(default_factory=list)
    scope: str = ""
    out_of_scope: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)


class DebugReport(BaseModel):
    root_cause: str = ""
    affected_files: list[str] = Field(default_factory=list)
    error_category: str = "unknown"


class JudgeDecision(BaseModel):
    decision: str = "REJECT"
    reason: str = ""
    strategy: str = ""
