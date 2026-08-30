"""
Pydantic schemas used by 2+ router modules. Every other schema is a
single-router concern and is defined directly in the router file that
uses it (see the Pydantic schemas -> router mapping done for the
main.py split for how each one was placed).
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class UserOut(BaseModel):
    id: int
    callsign: str
    gmrs_callsign: Optional[str] = None
    name: str
    email: str
    is_active: bool
    is_admin: bool
    notify_new_registrations: bool
    theme: str
    email_verified: bool
    created_at: datetime
    current_org_id: Optional[int] = None

    model_config = {"from_attributes": True}


class AdminUserOut(UserOut):
    """UserOut plus the user's current org's name/website — lets a super
    admin reviewing a pending registration (especially one founding a brand
    new org) verify it without a separate lookup (issue #1 follow-up)."""
    org_name: Optional[str] = None
    org_website_url: Optional[str] = None


class OrganizationOut(BaseModel):
    id: int
    name: str
    slug: str
    website_url: Optional[str] = None
    banner_message: Optional[str] = None   # org-admin-set, shown at the top of every page to this org's members

    model_config = {"from_attributes": True}


class NetOut(BaseModel):
    id: int
    name: str
    frequency: Optional[str]
    description: Optional[str]
    net_type: str
    is_ares: bool
    dmr_talkgroup: Optional[str] = None
    script: Optional[str] = None
    has_broadcast: bool = False
    broadcast_label: Optional[str] = None
    public_listed: bool = False
    aprs_map_enabled: bool = False
    reminder_enabled: bool = False
    reminder_minutes_before: Optional[int] = None
    band: Optional[str] = None
    mode: Optional[str] = None
    ctcss_tone: Optional[str] = None
    region: Optional[str] = None
    state: Optional[str] = None
    website: Optional[str] = None
    owner_id: int
    org_id: int
    created_at: datetime
    # Sharing fields (populated by helper, not from ORM attributes directly)
    is_owner: bool = True
    shared_with_all: bool = False
    shared_user_ids: list[int] = []
    can_edit_all: bool = False           # edit rights granted to the "shared with all" grant
    editor_user_ids: list[int] = []      # subset of shared_user_ids also granted edit rights
    can_edit: bool = False               # whether the CALLER (owner, admin, or an editor share) can edit this net
    owner_callsign: Optional[str] = None

    model_config = {"from_attributes": True}


class CheckinOut(BaseModel):
    id: int
    session_id: int
    callsign: str
    name: Optional[str]
    signal_report: Optional[str]
    comments: Optional[str]
    has_traffic: bool
    traffic_called: bool = False
    is_first_checkin: bool = False  # welcome first-time operators (net-level history, see _create_checkin)
    evac_zone: Optional[str]
    dmr_talkgroup: Optional[str] = None
    dmr_region: Optional[str] = None
    checked_in_at: datetime
    # Tactical position shift tracking (issue #21, activation sessions only).
    # tactical_callsign is denormalized from the linked TacticalPosition for
    # display — populated by list_checkins()/sign_on_tactical_position(), null
    # whenever the checkin isn't tied to a position.
    tactical_position_id: Optional[int] = None
    tactical_callsign: Optional[str] = None
    signed_off_at: Optional[datetime] = None
    # Manually-reported GPS position (issue follow-up), set independently of
    # check-in itself via PATCH /checkins/{id}/position -- both null if never set.
    lat: Optional[float] = None
    lon: Optional[float] = None

    model_config = {"from_attributes": True}
