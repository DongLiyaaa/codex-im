from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Login(Input):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator('email')
    @classmethod
    def email_normalize(cls, value):
        if '@' not in value:
            raise ValueError('Invalid email')
        return value.lower()


class SetupAdmin(Login):
    name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=1024)

    @field_validator('email')
    @classmethod
    def valid_email(cls, value):
        import re
        if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", value):
            raise ValueError('Invalid email')
        return value


class UserCreate(Login):
    name: str = Field(min_length=1, max_length=200)
    role: Literal['super_admin', 'org_admin', 'team_lead', 'member']
    org_id: str | None = Field(default=None, min_length=1, max_length=100)
    team_id: str | None = Field(default=None, min_length=1, max_length=100)
    active: bool = True
    password: str = Field(min_length=12, max_length=1024)

    @model_validator(mode='after')
    def tenant(self):
        if self.role != 'super_admin' and not self.org_id:
            raise ValueError('org_id required')
        if self.role in ('team_lead', 'member') and not self.team_id:
            raise ValueError('team_id required')
        if self.role == 'super_admin' and (self.org_id or self.team_id):
            raise ValueError('super_admin is global')
        return self


class UserUpdate(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    active: bool | None = None

    @model_validator(mode='after')
    def something(self):
        if self.name is None and self.active is None:
            raise ValueError('Nothing to update')
        return self


class GroupCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    org_id: str = Field(min_length=1, max_length=100)
    team_id: str | None = Field(default=None, min_length=1, max_length=100)
    member_ids: list[str] = Field(min_length=1, max_length=500)
    provider: Literal['web', 'feishu', 'dingtalk'] = 'web'
    external_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def valid(self):
        if len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError('Duplicate members')
        if self.provider != 'web' and not self.external_id:
            raise ValueError('external_id required for IM groups')
        return self


class GroupUpdate(Input):
    name: str = Field(min_length=1, max_length=200)
    member_ids: list[str] = Field(min_length=1, max_length=500)

    @field_validator('member_ids')
    @classmethod
    def unique_members(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('Duplicate members')
        return value


class ResourceCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal['skill', 'mcp']
    description: str = Field(default='', max_length=5000)
    org_id: str | None = Field(default=None, min_length=1, max_length=100)
    team_id: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool = True
    config: dict

    @model_validator(mode='after')
    def department_needs_organization(self):
        if self.team_id and not self.org_id:
            raise ValueError('部门范围的资源必须同时指定组织')
        return self


class BindingCreate(Input):
    subject_type: Literal['user', 'group']
    subject_id: str = Field(min_length=1, max_length=36)
    resource_id: str = Field(min_length=1, max_length=36)


class IdentityCreate(Input):
    provider: Literal['feishu', 'dingtalk']
    external_user_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=36)


class ConversationCreate(Input):
    title: str = Field(min_length=1, max_length=200)
    group_id: str | None = Field(default=None, min_length=1, max_length=36)


class MessageCreate(Input):
    content: str = Field(default='', max_length=16000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode='after')
    def nonempty(self):
        if not self.content and not self.attachment_ids:
            raise ValueError('请输入文本或添加附件')
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError('附件不能重复')
        return self


class Output(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(Output):
    id: str
    email: str
    name: str
    role: str
    org_id: str | None
    team_id: str | None
    active: bool
    login_enabled: bool = True
    can_manage: bool = False


class GroupOut(Output):
    can_edit: bool = False
    can_delete: bool = False
    org_name: str | None = None
    team_name: str | None = None
    id: str
    name: str
    org_id: str
    team_id: str | None
    member_ids: list[str]
    provider: str
    external_id: str | None


class ConversationOut(Output):
    can_delete: bool = False
    id: str
    title: str
    owner_id: str
    group_id: str | None
    created_at: datetime


class MessageOut(Output):
    platform_authorization: dict | None = None
    attachments: list[dict] = Field(default_factory=list)
    id: str
    conversation_id: str
    role: str
    content: str
    created_at: datetime


class RunOut(Output):
    id: str
    message_id: str
    status: str
    error: str | None
    created_at: datetime


class ConversationRunOut(RunOut):
    provider: str


class ConversationStateOut(Output):
    messages: list[MessageOut]
    active_run: ConversationRunOut | None
    latest_run: ConversationRunOut | None


class BindingOut(Output):
    id: str
    subject_type: str
    subject_id: str
    resource_id: str


class IdentityOut(Output):
    id: str
    provider: str
    external_user_id: str
    user_id: str
