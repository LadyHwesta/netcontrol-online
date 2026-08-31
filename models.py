"""
SQLAlchemy models for NetControl Online
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, DateTime, Date, Time, Float, ForeignKey, Text, Boolean, UniqueConstraint, TypeDecorator
)
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """DateTime(timezone=True) that guarantees a UTC-aware Python datetime on read,
    regardless of backend. Postgres's TIMESTAMPTZ round-trips tzinfo correctly on its
    own; SQLite has no real timestamp-with-timezone type, so a "timezone=True" column
    silently comes back naive there even though every value written was UTC (utcnow()).
    A naive datetime serializes to JSON with no offset marker, which browsers then
    parse as *local* time instead of UTC -- every timestamp in the app would render
    off by the viewer's UTC offset. Used everywhere DateTime(timezone=True) was used
    before; a no-op when the driver already returns a tz-aware value."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class User(Base):
    """Net control operators"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    callsign = Column(String(12), unique=True, nullable=False, index=True)
    gmrs_callsign = Column(String(12), nullable=True)  # separate GMRS family license, if held (issue #23)
    name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=False, nullable=False)  # False until admin approves
    is_admin = Column(Boolean, default=False, nullable=False)
    notify_new_registrations = Column(Boolean, default=False, nullable=False)  # email opt-in for new signups
    theme = Column(String(20), default="lcars", nullable=False)  # lcars | dark | light | high-contrast | system
    language = Column(String(10), nullable=True)  # ISO code (e.g. "es"); null = English / browser default
    email_verified = Column(Boolean, default=True, nullable=False)  # False only when SMTP is configured and a verification email was actually sent
    verification_token = Column(String(64), nullable=True)
    verification_sent_at = Column(UTCDateTime, nullable=True)
    # Set-password invite (admin-created accounts, issue #1 follow-up). Same
    # hash-only-stored pattern as verification_token above. hashed_password is
    # an unusable random placeholder until this is redeemed via /auth/set-password.
    password_set_token = Column(String(64), nullable=True)
    password_set_sent_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    # Multi-tenancy (issue #1). is_admin above is the *super admin* tier — unchanged,
    # still bypasses org scoping everywhere. current_org_id is which org this user is
    # "working as" right now; read fresh off this row each request (no JWT claim),
    # same as is_admin already is. Nullable only for the moment between registration
    # and the org create/join step completing; every active user ends up with one.
    current_org_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True)

    nets = relationship("Net", back_populates="owner", cascade="all, delete-orphan")
    sessions = relationship("NetSession", back_populates="operator")
    memberships = relationship("OrganizationMembership", back_populates="user", cascade="all, delete-orphan", foreign_keys="OrganizationMembership.user_id")

    def __repr__(self):
        return f"<User callsign={self.callsign}>"


class Organization(Base):
    """A tenant (issue #1) — e.g. one ARES section or region sharing a deployment
    with others but not each other's nets. Users join via OrganizationMembership;
    nets belong to exactly one org via Net.org_id."""
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    slug = Column(String(100), unique=True, nullable=False, index=True)  # URL-safe, used in /directory/{slug}, /live/{slug}
    # Required when creating a new org (enforced in main.py, not here) so a
    # super admin reviewing a founding registration has something to verify
    # the org against. Nullable at the DB level only so the pre-existing
    # "Default Organization" from the backward-compat migration doesn't need
    # a fabricated value.
    website_url = Column(String(300), nullable=True)
    # Org-admin-set announcement shown at the top of every authenticated page
    # to this org's members (issue follow-up — welcome messages). Distinct
    # from the instance-wide login/welcome-popup messages, which are
    # super-admin-set and stored in SystemSetting instead, since those apply
    # across every org rather than being a property of one.
    banner_message = Column(Text, nullable=True)
    # aprs.fi API key (issue follow-up), shared by every net in this org that
    # uses aprs_fi as its APRS source -- one key per org rather than
    # re-entered per net. Org-admin-set; deliberately NOT exposed on
    # OrganizationOut (unlike the fields above) since it's a real secret, not
    # a public-facing setting -- see routers/orgs.py's dedicated
    # GET/PUT /orgs/{id}/aprs-key for the org-admin-only read/write path.
    aprs_fi_api_key = Column(String(100), nullable=True)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)

    memberships = relationship("OrganizationMembership", back_populates="org", cascade="all, delete-orphan")
    nets = relationship("Net", back_populates="org")

    def __repr__(self):
        return f"<Organization {self.slug}>"


class OrganizationMembership(Base):
    """A user's membership in one organization (issue #1) — the org-admin tier,
    separate from User.is_admin (super admin, bypasses org scoping entirely).
    approved=False means pending: the org hasn't accepted this user yet, mirroring
    the existing User.is_active pending-approval flow but per-org instead of
    instance-wide. A user may hold multiple memberships (multiple orgs)."""
    __tablename__ = "organization_memberships"

    id = Column(Integer, primary_key=True, index=True)
    org_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False, default="member")  # 'admin' | 'member'
    approved = Column(Boolean, default=False, nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)

    org = relationship("Organization", back_populates="memberships")
    user = relationship("User", back_populates="memberships", foreign_keys=[user_id])

    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_org_membership_org_user"),
    )

    def __repr__(self):
        return f"<OrganizationMembership org={self.org_id} user={self.user_id} role={self.role}>"


class Net(Base):
    """A repeating amateur radio net"""
    __tablename__ = "nets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    frequency = Column(String(20), nullable=True)   # e.g. "146.520 MHz"
    description = Column(Text, nullable=True)
    net_type = Column(String(10), nullable=False, default='ham')  # 'ham' | 'gmrs'
    is_ares = Column(Boolean, default=False, nullable=False)  # ARES/ACES net — enables evac zone tracking (ham only)
    dmr_talkgroup = Column(String(20), nullable=True)  # Default DMR talk group for check-ins (ham only)
    script = Column(Text, nullable=True)  # Net control script, shown alongside the check-in screen
    has_broadcast = Column(Boolean, default=False, nullable=False)  # e.g. a Newsline segment carried during the net
    broadcast_label = Column(String(100), nullable=True)  # e.g. "Amateur Radio Newsline"
    reminder_enabled = Column(Boolean, default=False, nullable=False)  # email signed-up operators before net start
    reminder_minutes_before = Column(Integer, nullable=True)  # lead time in minutes, e.g. 30
    public_listed = Column(Boolean, default=False, nullable=False)  # shown in the public /directory (no login)
    aprs_map_enabled = Column(Boolean, default=False, nullable=False)  # shows an APRS station map on the public live page (issue #22)
    # Optional metadata — not used locally, only forwarded to Net Repository
    # (net_repository.py) to make the public directory listing more useful/searchable.
    band = Column(String(10), nullable=True)         # e.g. "2m", "70cm"
    mode = Column(String(20), nullable=True)          # e.g. "FM", "DMR"
    ctcss_tone = Column(String(10), nullable=True)     # e.g. "100.0"
    region = Column(String(100), nullable=True)        # e.g. "Snohomish County"
    state = Column(String(50), nullable=True)          # US state
    website = Column(String(300), nullable=True)       # falls back to org-wide branding website if unset
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Multi-tenancy (issue #1) — every net belongs to exactly one organization, set
    # from the creating user's current_org_id at creation time and never user-editable
    # directly. nullable=True only so existing rows can be backfilled by migrate.py's
    # default-org migration before the NOT NULL constraint is added.
    org_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)

    owner = relationship("User", back_populates="nets")
    org = relationship("Organization", back_populates="nets")
    sessions = relationship("NetSession", back_populates="net", cascade="all, delete-orphan")
    schedules = relationship("NetSchedule", back_populates="net", cascade="all, delete-orphan")
    evac_zones = relationship("EvacZone", back_populates="net", cascade="all, delete-orphan")
    shares = relationship("NetShare", back_populates="net", cascade="all, delete-orphan")
    dmr_config = relationship("DmrConfig", back_populates="net", uselist=False, cascade="all, delete-orphan")
    aprs_config = relationship("AprsConfig", back_populates="net", uselist=False, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Net name={self.name}>"


class NetSession(Base):
    """A single activation / running of a net"""
    __tablename__ = "net_sessions"

    id = Column(Integer, primary_key=True, index=True)
    net_id = Column(Integer, ForeignKey("nets.id", ondelete="CASCADE"), nullable=False)
    operator_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    name = Column(String(200), nullable=True)   # optional human-readable label
    notes = Column(Text, nullable=True)
    started_at = Column(UTCDateTime, default=utcnow, nullable=False)
    ended_at = Column(UTCDateTime, nullable=True)
    # Manual broadcaster override for this session — takes precedence over the schedule
    # sign-up for the session's date. Covers the case where the broadcaster isn't known
    # until the net is about to begin (issue #17).
    broadcaster_override_callsign = Column(String(20), nullable=True)
    broadcaster_override_name = Column(String(100), nullable=True)
    # ARES/ACES activation (issue #21) — set once at session start, immutable after.
    # A routine session on an ARES net (is_ares=true, is_activation=false) behaves
    # exactly as before; only an activation session gets tactical positions, shift
    # sign-on/off, and the simplified roster.
    is_activation = Column(Boolean, default=False, nullable=False)
    # Backfilled entry for a net that already happened with no access to the web
    # tool (issue #20) — created already "ended" (started_at/ended_at both set to
    # the reported date/time) so there's no live view, but still accepts checkins
    # (add_checkin()'s ended-session guard is bypassed for these specifically),
    # each stamped with started_at rather than utcnow(). ended_at can't double as
    # "no more checkins" for these (it's set from creation), so is_offline_locked
    # is the separate "finished entering data" signal — end_session() sets it
    # instead of ended_at for an offline session, and add_checkin() checks it
    # instead of ended_at.
    is_offline = Column(Boolean, default=False, nullable=False)
    is_offline_locked = Column(Boolean, default=False, nullable=False)
    # Manual Net Control override for this session — same precedence pattern as
    # broadcaster_override_* above. Mainly for offline entries, where whoever's
    # backfilling the log usually isn't who actually ran the net, but not
    # restricted to those.
    ncs_override_callsign = Column(String(20), nullable=True)
    ncs_override_name = Column(String(100), nullable=True)

    net = relationship("Net", back_populates="sessions")
    operator = relationship("User", back_populates="sessions")
    checkins = relationship("Checkin", back_populates="session", cascade="all, delete-orphan")
    traffic_messages = relationship("TrafficMessage", back_populates="session", cascade="all, delete-orphan")
    tactical_positions = relationship("TacticalPosition", back_populates="session", cascade="all, delete-orphan")
    net_control_shifts = relationship("NetControlShift", back_populates="session", cascade="all, delete-orphan")

    @property
    def is_active(self):
        return self.ended_at is None

    def __repr__(self):
        return f"<NetSession id={self.id} net={self.net_id}>"


class TacticalPosition(Base):
    """A tactical assignment slot for one ARES/ACES activation session (issue #21)
    — e.g. "SHELTER 1". Session-scoped and re-created fresh each activation, not a
    reusable net-level template: different activations commonly need entirely
    different tactical rosters. Who currently holds it, and its shift history, are
    derived from Checkin rows (tactical_position_id + signed_off_at), not stored here."""
    __tablename__ = "tactical_positions"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("net_sessions.id", ondelete="CASCADE"), nullable=False)
    tactical_callsign = Column(String(50), nullable=False)   # e.g. "SHELTER 1"
    location = Column(String(200), nullable=True)
    assigned_callsign = Column(String(12), nullable=True)    # planned/expected operator
    assigned_name = Column(String(100), nullable=True)
    scheduled_start = Column(UTCDateTime, nullable=True)   # planned shift sign-on time
    # Auto-created (one per activation session) to track Net Control itself through the
    # same sign-on/off/shift-history mechanism as any other position — NCS commonly hands
    # off mid-activation, unlike the single day-level schedule sign-up routine sessions use.
    # Not user-creatable and not deletable; enforced in main.py, not here.
    is_net_control = Column(Boolean, default=False, nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)

    session = relationship("NetSession", back_populates="tactical_positions")

    def __repr__(self):
        return f"<TacticalPosition {self.tactical_callsign}>"


class NetControlShift(Base):
    """A planned future Net Control shift for one activation session (issue #21
    follow-up). Kept separate from TacticalPosition.assigned_callsign/scheduled_start
    (a single "planned next" value) because Net Control classically rotates on a
    fixed cadence throughout a long activation -- operators want a whole rotation
    queued up in advance, not just one "who's next" slot. Handing off Net Control
    (via the auto-created is_net_control TacticalPosition's sign-on) pre-fills from
    whichever shift here has the earliest scheduled_start, then removes it -- this
    table is a forward-looking queue, not a permanent log; the actual handoff, and
    its history, lives on the TacticalPosition/Checkin side as always."""
    __tablename__ = "net_control_shifts"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("net_sessions.id", ondelete="CASCADE"), nullable=False)
    callsign = Column(String(12), nullable=False)
    name = Column(String(100), nullable=True)
    scheduled_start = Column(UTCDateTime, nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)

    session = relationship("NetSession", back_populates="net_control_shifts")

    def __repr__(self):
        return f"<NetControlShift {self.callsign} at {self.scheduled_start}>"


class Checkin(Base):
    """An individual station checking into a net session"""
    __tablename__ = "checkins"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("net_sessions.id", ondelete="CASCADE"), nullable=False)
    callsign = Column(String(12), nullable=False, index=True)
    name = Column(String(100), nullable=True)
    signal_report = Column(String(20), nullable=True)  # e.g. "59", "57"
    comments = Column(Text, nullable=True)
    has_traffic = Column(Boolean, default=False, nullable=False)
    traffic_called = Column(Boolean, default=False, nullable=False)  # operator has passed this station's traffic
    # True if this callsign had never checked into this net (any session) before
    # this row — computed once at creation time in _create_checkin(), not
    # recomputed later, so it stays historically accurate even if earlier
    # check-ins are later deleted. Lets net control welcome first-timers.
    is_first_checkin = Column(Boolean, default=False, nullable=False)
    evac_zone = Column(String(100), nullable=True)   # ARES/ACES evacuation zone
    dmr_talkgroup = Column(String(20), nullable=True)  # DMR talk group, e.g. "3100"
    dmr_region = Column(String(100), nullable=True)    # Region/state/area for DMR nets
    checked_in_at = Column(UTCDateTime, default=utcnow, nullable=False)
    # Tactical position shift tracking (issue #21, activation sessions only). Each
    # sign-on is its own Checkin row — checked_in_at is the shift's start, and
    # signed_off_at (once set) is its end. A position's current occupant is the
    # checkin with tactical_position_id set and signed_off_at still null; earlier
    # rows for the same position are that position's shift history, kept for free.
    tactical_position_id = Column(Integer, ForeignKey("tactical_positions.id", ondelete="SET NULL"), nullable=True)
    signed_off_at = Column(UTCDateTime, nullable=True)
    # Manually-reported GPS position (issue follow-up) -- for an operator with
    # no APRS capability but who can read off their own coordinates. Set
    # after check-in via PATCH /checkins/{id}/position, not at check-in time
    # itself (keeps the fast check-in form uncluttered); shown on the same
    # station map as APRS-derived positions, tagged as "manual" there.
    lat = Column(Float, nullable=True)
    lon = Column(Float, nullable=True)

    session = relationship("NetSession", back_populates="checkins")

    # No DB-level unique constraint on (session_id, callsign) — GMRS nets allow the
    # same callsign multiple times (shared family licence). Uniqueness for ham nets
    # is enforced at the application layer in add_checkin(). Tactical sign-ons
    # (sign_on_tactical_position()) bypass that check entirely — the same operator
    # legitimately holding two positions, or re-signing onto one later, is expected.

    def __repr__(self):
        return f"<Checkin callsign={self.callsign} session={self.session_id}>"


class EvacZone(Base):
    """Tracks the most recent evacuation zone reported by each callsign for a given net."""
    __tablename__ = "evac_zones"

    id = Column(Integer, primary_key=True, index=True)
    net_id = Column(Integer, ForeignKey("nets.id", ondelete="CASCADE"), nullable=False)
    callsign = Column(String(12), nullable=False, index=True)
    zone = Column(String(100), nullable=False)
    updated_at = Column(UTCDateTime, default=utcnow, nullable=False)

    net = relationship("Net", back_populates="evac_zones")

    __table_args__ = (
        UniqueConstraint("net_id", "callsign", name="uq_evac_zone_net_callsign"),
    )

    def __repr__(self):
        return f"<EvacZone callsign={self.callsign} zone={self.zone}>"


class TrafficMessage(Base):
    """A formal or informal traffic message handled during a net session."""
    __tablename__ = "traffic_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("net_sessions.id", ondelete="CASCADE"), nullable=False)
    msg_number = Column(String(50), nullable=True)          # e.g. "NTS-001", "ICS-214-7"
    origin_callsign = Column(String(12), nullable=False)
    dest_info = Column(String(200), nullable=True)           # destination callsign or address
    msg_type = Column(String(20), nullable=False, default="formal")   # formal | informal | health_welfare
    status = Column(String(20), nullable=False, default="received")   # received | relayed | delivered | undeliverable
    notes = Column(Text, nullable=True)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)

    session = relationship("NetSession", back_populates="traffic_messages")

    def __repr__(self):
        return f"<TrafficMessage #{self.msg_number} from={self.origin_callsign}>"


class StationRemark(Base):
    """Persistent operator notes (and preferred name) about a callsign, scoped to a net."""
    __tablename__ = "station_remarks"

    id = Column(Integer, primary_key=True, index=True)
    callsign = Column(String(12), nullable=False, index=True)
    net_id = Column(Integer, ForeignKey("nets.id", ondelete="CASCADE"), nullable=False)
    remark = Column(Text, nullable=True)
    # Overrides the FCC/callsign-lookup name in the Expected Stations list and net
    # reports (ICS-205, CSV exports) — not the check-in record itself.
    preferred_name = Column(String(100), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("callsign", "net_id", name="uq_station_remark_callsign_net"),
    )

    def __repr__(self):
        return f"<StationRemark callsign={self.callsign} net={self.net_id}>"


class NetShare(Base):
    """Grants a registered user access to a net they don't own.
    user_id=NULL means the net is shared with ALL registered users."""
    __tablename__ = "net_shares"

    id = Column(Integer, primary_key=True, index=True)
    net_id = Column(Integer, ForeignKey("nets.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)  # NULL = share with all
    # Whether this share also grants edit rights (net details, schedule, DMR
    # config, evac zones, station remarks) rather than just view/check-in
    # access (issue follow-up). Deleting the net and managing sharing itself
    # stay owner/admin-only regardless.
    can_edit = Column(Boolean, default=False, nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)

    net = relationship("Net", back_populates="shares")

    __table_args__ = (
        UniqueConstraint("net_id", "user_id", name="uq_net_share_net_user"),
    )

    def __repr__(self):
        target = f"user={self.user_id}" if self.user_id else "ALL"
        return f"<NetShare net={self.net_id} {target}>"


class NetSchedule(Base):
    """Weekly recurring schedule for a net (e.g. every Monday at 19:30)"""
    __tablename__ = "net_schedules"

    id = Column(Integer, primary_key=True, index=True)
    net_id = Column(Integer, ForeignKey("nets.id", ondelete="CASCADE"), nullable=False)
    # 0=Monday … 6=Sunday  (matches Python datetime.weekday())
    day_of_week = Column(Integer, nullable=False)
    start_time = Column(String(5), nullable=False)   # "HH:MM" in local tz
    timezone = Column(String(60), nullable=False, default="UTC")
    notes = Column(Text, nullable=True)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)

    net = relationship("Net", back_populates="schedules")
    signups = relationship("NetControlSignup", back_populates="schedule", cascade="all, delete-orphan")

    def __repr__(self):
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"<NetSchedule {days[self.day_of_week]} {self.start_time}>"


class SystemSetting(Base):
    """Key-value store for site-wide configuration such as branding."""
    __tablename__ = "system_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def __repr__(self):
        return f"<SystemSetting key={self.key}>"


class NetControlSignup(Base):
    """A logged-in operator claiming net control for a specific upcoming date"""
    __tablename__ = "net_control_signups"

    id = Column(Integer, primary_key=True, index=True)
    schedule_id = Column(Integer, ForeignKey("net_schedules.id", ondelete="CASCADE"), nullable=False)
    net_id = Column(Integer, ForeignKey("nets.id", ondelete="CASCADE"), nullable=False)
    slot_date = Column(Date, nullable=False)          # the specific date (YYYY-MM-DD)
    # 'net_control' | 'broadcaster' | 'both' — 'both' means this one signup covers both duties
    role = Column(String(20), nullable=False, default="net_control")
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    callsign = Column(String(12), nullable=False)
    name = Column(String(100), nullable=True)
    email = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    signed_up_at = Column(UTCDateTime, default=utcnow, nullable=False)
    reminder_sent_at = Column(UTCDateTime, nullable=True)  # set once a reminder email has gone out

    schedule = relationship("NetSchedule", back_populates="signups")

    __table_args__ = (
        # One signup per schedule date per role (net_control and broadcaster fill independently;
        # 'both' occupies the date exclusively — enforced at the application layer)
        UniqueConstraint("schedule_id", "slot_date", "role", name="uq_signup_schedule_date_role"),
    )

    def __repr__(self):
        return f"<NetControlSignup {self.callsign} on {self.slot_date}>"


class ApiToken(Base):
    """Long-lived tokens for service accounts (e.g. DMR relay scripts)."""
    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)             # human label, e.g. "DMR Relay - shack Pi"
    token_hash = Column(String(64), nullable=False, unique=True)  # SHA-256 hex of the raw token
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    last_used_at = Column(UTCDateTime, nullable=True)

    user = relationship("User")

    def __repr__(self):
        return f"<ApiToken name={self.name} user={self.user_id}>"


class GmrsLicense(Base):
    """
    Local copy of the FCC ULS GMRS (service ZA) database.
    Populated by gmrs_sync.py; updated weekly from the FCC bulk download.
    """
    __tablename__ = "gmrs_licenses"

    callsign = Column(String(16), primary_key=True)
    licensee_name = Column(String(200), nullable=True)   # full name or entity name
    state = Column(String(50), nullable=True)
    expires = Column(String(20), nullable=True)           # raw date string from FCC (MM/DD/YYYY)
    status = Column(String(4), nullable=True)             # 'A'=Active, 'E'=Expired, etc.
    synced_at = Column(UTCDateTime, nullable=False, default=utcnow)

    def __repr__(self):
        return f"<GmrsLicense {self.callsign} {self.licensee_name}>"


class CallsignCache(Base):
    """Local cache of FCC/external callsign lookups to reduce external API dependency."""
    __tablename__ = "callsign_cache"

    callsign = Column(String(12), primary_key=True)
    status = Column(String(10), nullable=False)          # "found" | "not_found"
    name = Column(String(200), nullable=True)
    license_class = Column(String(10), nullable=True)
    state = Column(String(10), nullable=True)
    grid = Column(String(10), nullable=True)
    expires = Column(String(20), nullable=True)
    source = Column(String(50), nullable=True)
    cached_at = Column(UTCDateTime, nullable=False, default=utcnow)

    def __repr__(self):
        return f"<CallsignCache {self.callsign} status={self.status}>"


class DmrConfig(Base):
    """Per-net digital voice last-heard integration configuration (hotspot
    or network API). Despite the class/table name (kept for API/migration
    stability), this covers DMR and other digital voice modes (issue #26)
    — a WPSD/Pi-Star hotspot's last-heard feed reports whichever mode(s)
    it hears, tagged per-entry; `mode` below just narrows what's shown."""
    __tablename__ = "dmr_configs"

    id = Column(Integer, primary_key=True, index=True)
    net_id = Column(Integer, ForeignKey("nets.id", ondelete="CASCADE"), nullable=False, unique=True)
    # wpsd | pistar | brandmeister
    source_type = Column(String(20), nullable=False, default="wpsd")
    # dmr | dstar | ysf | nxdn | p25 | m17 (issue #26). BrandMeister is
    # DMR-only, enforced server-side. Existing rows default to "dmr" to
    # preserve behavior from before other modes existed.
    mode = Column(String(10), nullable=False, default="dmr")
    # WPSD/Pi-Star: full API URL, e.g. http://wpsd.local/api or http://host/api/last_heard.php
    hotspot_url = Column(Text, nullable=True)
    # BrandMeister: talk group number to monitor
    talkgroup_id = Column(Integer, nullable=True)
    # Callsign to exclude from heard list (usually NCS operator)
    filter_callsign = Column(String(12), nullable=True)
    # True = browser fetches hotspot directly (for local-network hotspots)
    # False = backend proxies the request (for public/accessible URLs)
    direct_mode = Column(Boolean, nullable=False, default=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)

    net = relationship("Net", back_populates="dmr_config")

    def __repr__(self):
        return f"<DmrConfig net={self.net_id} type={self.source_type}>"


class AprsConfig(Base):
    """Per-net APRS station-map integration configuration (issue #22).
    Mirrors DmrConfig's shape — one row per net, presence of the row is the
    on/off switch (no separate boolean)."""
    __tablename__ = "aprs_configs"

    id = Column(Integer, primary_key=True, index=True)
    net_id = Column(Integer, ForeignKey("nets.id", ondelete="CASCADE"), nullable=False, unique=True)
    # aprs_fi | relay
    source_type = Column(String(20), nullable=False, default="relay")
    # aprs.fi: the API key itself is org-level now (Organization.aprs_fi_api_key,
    # issue follow-up -- multi-tenant instances shouldn't need the same key
    # re-entered into every net in an org), not stored per-net any more.
    # Callsign to exclude from the map (usually NCS operator)
    filter_callsign = Column(String(12), nullable=True)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)

    net = relationship("Net", back_populates="aprs_config")

    def __repr__(self):
        return f"<AprsConfig net={self.net_id} type={self.source_type}>"


class TranslationCache(Base):
    """Translation memory for the argos-translate integration -- the English
    source text itself is the cache key (hashed), not an invented key name,
    same principle gettext/_() has used for decades. One row serves both UI
    chrome strings and on-demand user-content translation (net scripts,
    welcome messages, announcements) -- same operation either way."""
    __tablename__ = "translation_cache"

    id = Column(Integer, primary_key=True, index=True)
    cache_key = Column(String(64), unique=True, nullable=False, index=True)  # sha256(lang|context|source_text)
    target_lang = Column(String(10), nullable=False, index=True)
    source_text = Column(Text, nullable=False)
    translated_text = Column(Text, nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)

    def __repr__(self):
        return f"<TranslationCache lang={self.target_lang} key={self.cache_key[:8]}>"


class EnabledLanguage(Base):
    """A language a super admin has turned on for the UI translation feature.
    Presence of a 'ready' row is what the language switcher and browser-
    language auto-detect actually offer -- argos-translate itself supports
    many more languages than any given instance has chosen to enable."""
    __tablename__ = "enabled_languages"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(10), unique=True, nullable=False)  # "es", "fr", ...
    display_name = Column(String(50), nullable=False)  # "Español"
    # pending (just created) | installing (model download/pretranslate running) | ready | error
    model_status = Column(String(20), nullable=False, default="pending")
    error_message = Column(Text, nullable=True)
    enabled_at = Column(UTCDateTime, default=utcnow, nullable=False)

    def __repr__(self):
        return f"<EnabledLanguage {self.code} status={self.model_status}>"
