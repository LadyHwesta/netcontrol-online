// ============================================================
// CONFIG
// ============================================================
const API = '';

// ============================================================
// STATE
// ============================================================
let token = localStorage.getItem('nt_token') || null;
let currentUser = null;
let nets = [];
let currentNetId = null;
let currentNetOwnerId = null;
let currentNetIsAres = false;
let currentNetIsGmrs = false;
let currentSessionId = null;
let currentSessionIsActivation = false;   // ARES/ACES activation session (issue #21)
let tacticalPositions = [];   // loaded from /sessions/{id}/tactical-positions when on that tab
let currentSessionData = null;   // last-fetched session object; re-used to re-render the duty
                                  // bar/net script when a Net Control handoff changes who's on (issue #21 follow-up)
let netControlShifts = [];   // planned Net Control rotation, loaded from /sessions/{id}/net-control-shifts
let activeView = 'nets';
let historyData = [];
let editNetId = null;
let allUsers = [];   // for sharing UI — populated when opening edit form
let shareState = { share_with_all: false, can_edit_all: false, user_ids: [], editor_user_ids: [] };
let evacZones = {};   // callsign → zone, loaded from /nets/{id}/evac-zones
let currentNetScript = null;   // raw (unrendered) net.script text for the open net
