"""DocuFlow - Complete Pydantic Schemas with full validation."""
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime
from uuid import UUID
from enum import Enum


class ModuleEnum(str, Enum):
    extraction = "extraction"
    biometric  = "biometric"
    validation = "validation"
    fuzzy      = "fuzzy"


class PriorityEnum(str, Enum):
    high   = "high"
    normal = "normal"
    low    = "low"


class TokenRequest(BaseModel):
    client_id:     str = Field(..., min_length=3)
    client_secret: str = Field(..., min_length=8)
    grant_type:    str = "client_credentials"


class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "Bearer"
    expires_in:   int


class FieldResult(BaseModel):
    value:      Optional[Any]  = None
    confidence: float          = Field(ge=0.0, le=1.0)
    alerts:     List[str]      = Field(default_factory=list)


class BiometricResult(BaseModel):
    face_detected_document:  bool
    face_count_document:     Optional[int]   = None
    face_detected_selfie:    Optional[bool]  = None
    face_count_selfie:       Optional[int]   = None
    similarity_score:        Optional[float] = Field(None, ge=0.0, le=1.0)
    decision:                Optional[str]   = None
    threshold_used:          float
    liveness_score:          Optional[float] = None
    liveness_result:         Optional[str]   = None
    photo_on_document:       bool
    photo_integrity_score:   Optional[float] = None
    spoofing_attempt:        bool = False
    processing_time_ms:      int


class FuzzyFieldResult(BaseModel):
    extracted:  str
    reference:  str
    score:      float
    algorithm:  str
    threshold:  float
    decision:   str


class FuzzyResult(BaseModel):
    fields:       Dict[str, FuzzyFieldResult]
    global_score: float
    overall:      str


class ValidationError(BaseModel):
    field:    str
    message:  str
    severity: str = "error"


class ValidationResult(BaseModel):
    passed:        bool
    rules_checked: int
    rules_failed:  int
    errors:        List[ValidationError] = Field(default_factory=list)
    warnings:      List[ValidationError] = Field(default_factory=list)


class ProcessResponse(BaseModel):
    document_id:        str
    trace_id:           str
    status:             str
    template_id:        Optional[str]
    document_type:      Optional[str]
    processing_time_ms: Optional[int]
    overall_confidence: Optional[float]
    global_decision:    Optional[str]
    fields:             Optional[Dict[str, FieldResult]]  = None
    biometric_check:    Optional[BiometricResult]         = None
    validation:         Optional[ValidationResult]        = None
    fuzzy_matching:     Optional[FuzzyResult]             = None
    alerts:             List[str]                         = Field(default_factory=list)
    mrz_decoded:        Optional[Dict[str, Any]]          = None
    created_at:         datetime

    model_config = {"from_attributes": True}


class BatchDocumentItem(BaseModel):
    url:            str
    template_id:    Optional[str] = None
    reference_data: Optional[Dict[str, Any]] = None


class BatchRequest(BaseModel):
    documents:   List[BatchDocumentItem] = Field(..., min_length=1, max_length=10000)
    webhook_url: Optional[str]   = None
    batch_id:    Optional[str]   = None
    modules:     List[ModuleEnum] = Field(default=[ModuleEnum.extraction, ModuleEnum.validation, ModuleEnum.fuzzy])
    priority:    PriorityEnum    = PriorityEnum.normal


class BatchResponse(BaseModel):
    batch_id:                     str
    total_documents:              int
    status:                       str
    estimated_completion_seconds: int
    tracking_url:                 str


class TemplateFieldValidation(BaseModel):
    required:   bool           = False
    min_length: Optional[int]  = None
    max_length: Optional[int]  = None
    regex:      Optional[str]  = None
    min_age:    Optional[int]  = None
    not_future: bool           = False
    not_past:   bool           = False
    severity:   str            = "error"


class TemplateField(BaseModel):
    id:              str
    label:           str
    type:            str
    zone:            Optional[Dict[str, float]] = None
    format:          Optional[str]              = None
    validation:      TemplateFieldValidation    = Field(default_factory=TemplateFieldValidation)
    ocr_tolerance:   float = 0.85
    fuzzy_threshold: float = 0.90


class TemplateCreate(BaseModel):
    id:            str
    name:          str
    document_type: str
    country:       Optional[str] = None
    fields:        List[TemplateField]


class TemplateUpdate(BaseModel):
    name:          Optional[str]              = None
    document_type: Optional[str]              = None
    country:       Optional[str]              = None
    fields:        Optional[List[TemplateField]] = None
    active:        Optional[bool]             = None


class TemplateResponse(BaseModel):
    id:            str
    name:          str
    document_type: str
    country:       Optional[str]
    version:       str
    active:        bool
    fields_count:  int
    created_at:    datetime

    model_config = {"from_attributes": True}


class StatsResponse(BaseModel):
    total_documents:        int
    today_documents:        int
    success_rate:           float
    avg_processing_time_ms: float
    active_workers:         int
    queue_depth:            int
    dlq_depth:              int = 0
    documents_by_status:    Dict[str, int]
    documents_by_type:      Dict[str, int]


class AuditLogResponse(BaseModel):
    id:         UUID
    action:     str
    client_id:  Optional[str]
    detail:     Dict[str, Any]
    ip_address: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class PaginatedResponse(BaseModel):
    items: List[Any]
    total: int
    page:  int
    size:  int
    pages: int

    @classmethod
    def create(cls, items, total, page, size):
        import math
        return cls(items=items, total=total, page=page, size=size,
                   pages=math.ceil(total / size) if size > 0 else 0)
