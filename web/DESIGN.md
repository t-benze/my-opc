---
# DESIGN.md — HappyRanch Founder Console
# Format: google-labs/design.md (YAML frontmatter machine-readable tokens
# + canonical markdown sections below). Token references use {dot.path}.
#
# Owner: the one human founder running this org. Everything is a single
# desktop SPA, served on localhost, used daily, for years.
#
# NOTE (2026-06-20, THR-030 / TASK-603): The live token system now uses
# Direction-A "Pasture" (light-first, warm, green accent). The palette
# below was the original dark-first / blue "Mission Control" lineage and
# is retained as the DESIGN.md specification reference. The implementation
# tokens.css diverges per the Pasture foundation; the semantic token
# NAMES are identical, only VALUES changed.

name: HappyRanch Founder Console
version: 0.1.0
description: >
  A calm, dense, slightly editorial cockpit for a single human supervising
  many autonomous AI agents. Email-client posture, ops-console density,
  agent-telemetry legibility.

# -------------------------------------------------------------------- #
# 1. Colors — semantic over decorative.                                #
# -------------------------------------------------------------------- #
# Two modes share component tokens; only the palette swaps. Dark is the
# v1 default. Light is designed-first so we cannot paint ourselves into a
# dark-only corner later.

colors:
  # Raw palette — never used directly in components. Reference via the
  # semantic tokens below.
  palette:
    # Inks (warm, near-neutral; not pure black/white — pure neutrals look
    # cheap on calibrated panels and hurt eyes at desk distance).
    ink_950: "#0c0d0f"   # darkest canvas
    ink_900: "#121317"
    ink_850: "#171920"
    ink_800: "#1d1f27"
    ink_700: "#262932"
    ink_600: "#363a45"
    ink_500: "#4c515e"
    ink_400: "#6d7280"
    ink_300: "#9aa0ac"
    ink_200: "#c4c8d0"
    ink_100: "#e7e8ec"
    ink_050: "#f3f4f6"
    ink_025: "#fafbfc"   # lightest canvas
    paper:   "#fdfdfb"   # warm white for light mode — sub-perceptual yellow
                          # cast keeps it editorial, not clinical

    # Brand — a single saturated cyan-blue, dialed for AA on both modes.
    # Chosen over a default vercel-blue/linear-purple to feel adjacent to
    # broadcast terminals, ham-radio dials, and ASCII manuals. Editorial,
    # not techy.
    signal_700: "#0e5a86"
    signal_600: "#1078b0"
    signal_500: "#1894d3"   # accent.default (dark mode)
    signal_400: "#3aabe3"
    signal_300: "#6dc1ec"
    signal_200: "#a8dbf3"
    signal_100: "#dbeefb"

    # Tier palette — green/yellow/red, desaturated by ~15 percent vs
    # Tailwind defaults so dense tables of status badges do not vibrate.
    green_700: "#1e7a4c"
    green_500: "#2ea36a"
    green_300: "#6fcea3"
    green_100: "#d7f0e3"

    amber_700: "#a07107"
    amber_500: "#caa72a"
    amber_300: "#e4cf6e"
    amber_100: "#f6ecc4"

    red_700:   "#a3372b"
    red_500:   "#d04a3a"
    red_300:   "#e88a7d"
    red_100:   "#f5d8d3"

    # Agent identity dots — one warm violet (manager), one cool teal
    # (worker). Deliberately NOT mapped to tier colors — role identity is
    # orthogonal to status meaning.
    violet_500: "#8265c8"
    violet_300: "#b6a3e1"
    teal_500:   "#2a9a93"
    teal_300:   "#7cc4be"

  # Semantic tokens — these are the only names components reference.
  semantic:
    dark:
      surface:
        canvas:        "{colors.palette.ink_950}"   # body bg, behind everything
        sunken:        "{colors.palette.ink_900}"   # inbox column, sidebars
        raised:        "{colors.palette.ink_850}"   # cards, rows, inputs
        overlay:       "{colors.palette.ink_800}"   # modals, popovers, drawers
        scrim:         "rgba(0, 0, 0, 0.55)"        # modal backdrop
      text:
        primary:       "{colors.palette.ink_100}"   # body text, message bodies
        secondary:     "{colors.palette.ink_300}"   # meta, labels, timestamps
        muted:         "{colors.palette.ink_400}"   # placeholders, disabled labels
        inverse:       "{colors.palette.ink_900}"   # text on accent fills
      border:
        subtle:        "{colors.palette.ink_800}"   # divider between siblings
        default:       "{colors.palette.ink_700}"   # input border, card edge
        strong:        "{colors.palette.ink_600}"   # focused / hovered border
      accent:
        default:       "{colors.palette.signal_500}"
        hover:         "{colors.palette.signal_400}"
        muted:         "rgba(24, 148, 211, 0.12)"   # signal_500 @ 12% — tinted bg
        ring:          "rgba(24, 148, 211, 0.45)"   # focus ring
      tier:
        green:         "{colors.palette.green_500}"
        green_tint:    "rgba(46, 163, 106, 0.14)"
        yellow:        "{colors.palette.amber_500}"
        yellow_tint:   "rgba(202, 167, 42, 0.14)"
        red:           "{colors.palette.red_500}"
        red_tint:      "rgba(208, 74, 58, 0.14)"
      agent:
        manager:       "{colors.palette.violet_500}"   # 6px dot
        worker:        "{colors.palette.teal_500}"
        founder:       "{colors.palette.signal_400}"   # founder is "you"
      status:
        open:          "{colors.palette.green_500}"
        archiving:     "{colors.palette.amber_500}"
        archived:      "{colors.palette.ink_400}"   # greyed — terminal, neutral
        abandoned:     "{colors.palette.red_500}"
        blocked:       "{colors.palette.amber_500}"
        escalated:     "{colors.palette.red_500}"
      id:
        thread:        "{colors.palette.signal_300}"   # THR-NNN monospace tint
        task:          "{colors.palette.violet_300}"   # TASK-NNN monospace tint
      feedback:
        success:       "{colors.palette.green_500}"
        warning:       "{colors.palette.amber_500}"
        danger:        "{colors.palette.red_500}"
        info:          "{colors.palette.signal_400}"

    light:
      surface:
        canvas:        "{colors.palette.paper}"
        sunken:        "{colors.palette.ink_050}"
        raised:        "#ffffff"
        overlay:       "#ffffff"
        scrim:         "rgba(12, 13, 15, 0.35)"
      text:
        primary:       "{colors.palette.ink_900}"
        secondary:     "{colors.palette.ink_600}"
        muted:         "{colors.palette.ink_500}"
        inverse:       "{colors.palette.ink_025}"
      border:
        subtle:        "{colors.palette.ink_100}"
        default:       "{colors.palette.ink_200}"
        strong:        "{colors.palette.ink_300}"
      accent:
        default:       "{colors.palette.signal_600}"
        hover:         "{colors.palette.signal_700}"
        muted:         "rgba(16, 120, 176, 0.10)"
        ring:          "rgba(16, 120, 176, 0.35)"
      tier:
        green:         "{colors.palette.green_700}"
        green_tint:    "rgba(30, 122, 76, 0.10)"
        yellow:        "{colors.palette.amber_700}"
        yellow_tint:   "rgba(160, 113, 7, 0.10)"
        red:           "{colors.palette.red_700}"
        red_tint:      "rgba(163, 55, 43, 0.10)"
      agent:
        manager:       "{colors.palette.violet_500}"
        worker:        "{colors.palette.teal_500}"
        founder:       "{colors.palette.signal_600}"
      status:
        open:          "{colors.palette.green_700}"
        archiving:     "{colors.palette.amber_700}"
        archived:      "{colors.palette.ink_500}"
        abandoned:     "{colors.palette.red_700}"
        blocked:       "{colors.palette.amber_700}"
        escalated:     "{colors.palette.red_700}"
      id:
        thread:        "{colors.palette.signal_600}"
        task:          "{colors.palette.violet_500}"
      feedback:
        success:       "{colors.palette.green_700}"
        warning:       "{colors.palette.amber_700}"
        danger:        "{colors.palette.red_700}"
        info:          "{colors.palette.signal_600}"

# -------------------------------------------------------------------- #
# 2. Typography — two families. Generous body, dense meta, mono-first  #
#    for everything machine-identified (IDs, timestamps, tokens).      #
# -------------------------------------------------------------------- #

typography:
  families:
    # Sans: Public Sans (USWDS open-source). Slightly humanist, designed
    # for long-form government/editorial layouts, renders cleanly at small
    # sizes. NOT Inter (over-used, geometric, no point of view at body
    # weight). NOT Söhne (licensed). NOT system-ui (looks default).
    sans:
      stack: '"Public Sans", "Public Sans Local", -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, sans-serif'
      weights: [400, 500, 600, 700]
    # Mono: JetBrains Mono. Distinct from the SF Mono / Menlo default the
    # browser would give us, which we'd never notice. JetBrains has a
    # taller x-height than Fira Code and tighter columns than IBM Plex
    # Mono — better density in tight badges.
    mono:
      stack: '"JetBrains Mono", "JetBrains Mono Local", ui-monospace, "SF Mono", Menlo, Consolas, monospace'
      weights: [400, 500, 600]
    # No serif. We considered one for an editorial banner, rejected: an
    # ops console with a serif headline looks like a magazine pretending
    # to be software. Keep it honest.

  scale:
    # All scale steps name a use, not a size. Sizes are in rem so the
    # OS-level zoom respects them.
    display:   { size: "1.875rem", line: "2.25rem",  weight: 600, tracking: "-0.01em" } # page H1 reserved for placeholders
    h1:        { size: "1.375rem", line: "1.75rem",  weight: 600, tracking: "-0.005em" } # screen titles
    h2:        { size: "1.125rem", line: "1.5rem",   weight: 600, tracking: "0" }
    h3:        { size: "1rem",     line: "1.5rem",   weight: 600, tracking: "0" }
    body_lg:   { size: "0.9375rem", line: "1.5rem",  weight: 400, tracking: "0" }     # message bodies — long-form reading
    body:      { size: "0.875rem", line: "1.375rem", weight: 400, tracking: "0" }     # default UI text
    body_sm:   { size: "0.8125rem", line: "1.25rem", weight: 400, tracking: "0" }     # inbox rows, dense tables
    label:     { size: "0.75rem",  line: "1rem",     weight: 500, tracking: "0.02em" }  # form labels (NOT all-caps by default)
    overline:  { size: "0.6875rem", line: "1rem",    weight: 600, tracking: "0.08em", transform: "uppercase" } # section headers in left column
    caption:   { size: "0.75rem",  line: "1rem",     weight: 400, tracking: "0" }     # timestamps, meta
    mono_md:   { size: "0.8125rem", line: "1.25rem", weight: 500, tracking: "0", family: "mono" } # IDs in headers
    mono_sm:   { size: "0.6875rem", line: "1rem",    weight: 500, tracking: "0", family: "mono" } # IDs in row badges, kbd chips
    code:      { size: "0.8125rem", line: "1.5rem",  weight: 400, tracking: "0", family: "mono" } # inline code in message bodies

  # The Composer textarea and message bodies use body_lg with measure
  # capped at 72ch — message bodies are read, not glanced at.
  measure:
    reading: "72ch"
    ui:      "none"     # UI text never wraps to a measure; it lays out by grid

# -------------------------------------------------------------------- #
# 3. Layout — 8pt grid, with 4pt for hairline alignment of badges.     #
# -------------------------------------------------------------------- #

layout:
  spacing:
    "0":  "0"
    "1":  "0.25rem"   #  4px — only used for kbd chip insets, badge padding
    "2":  "0.5rem"    #  8px — gap between inline elements
    "3":  "0.75rem"   # 12px — gap between message bubbles
    "4":  "1rem"      # 16px — pane/row padding
    "5":  "1.5rem"    # 24px — between sibling sections
    "6":  "2rem"      # 32px — between major screen regions
    "7":  "3rem"      # 48px — empty-state vertical padding
    "8":  "4rem"      # 64px — reserved; rarely used in a dense product

  radius:
    none:   "0"
    sm:     "0.1875rem"  # 3px — kbd chips, badges. Small. Editorial. NOT pill.
    md:     "0.3125rem"  # 5px — buttons, inputs, rows
    lg:     "0.5rem"     # 8px — cards, modals, panels
    pill:   "999px"      # used only on badges, chips, tabs, tier dots, and status indicators

  grid:
    app_shell:
      rows: "48px 1fr 24px"          # topbar / body / statusbar
      cols: "1fr"
    threads_page:
      cols: "340px 1fr"               # inbox / detail
      gap:  "0"                       # uses a single divider, not a gap
    dashboard:                        # used for future Tasks/Audit/Trends
      cols: "240px 1fr"               # left nav / canvas
      gap:  "{layout.spacing.5}"

  breakpoints:
    # Founder workstation only. We still set breakpoints so a sidecar
    # browser on a laptop does not collapse entirely.
    sm:  "768px"
    md:  "1024px"
    lg:  "1280px"   # design target
    xl:  "1600px"   # large external display
    xxl: "1920px"

  density:
    comfortable:                       # default
      row_height:    "44px"            # InboxRow
      message_pad:   "{layout.spacing.4}"
      input_pad_y:   "{layout.spacing.2}"
    compact:                           # toggleable; for long inbox days
      row_height:    "32px"
      message_pad:   "{layout.spacing.3}"
      input_pad_y:   "0.375rem"

# -------------------------------------------------------------------- #
# 4. Elevation & Depth — modest. This is a flat product.               #
# -------------------------------------------------------------------- #

elevation:
  # No drop shadows on resting cards. Depth is conveyed by surface tint
  # difference (sunken < raised < overlay). Shadows reserved for floating
  # surfaces (modal, popover, toast).
  "0":
    description: "Flush with parent. Used by InboxRow, MessageBubble at rest."
    shadow: "none"
    bg: "{colors.semantic.dark.surface.raised}"
  "1":
    description: "Header bands, ThreadHeader, TopBar — visually separates without floating."
    shadow: "0 1px 0 rgba(0, 0, 0, 0.35)"
    bg: "{colors.semantic.dark.surface.sunken}"
  "2":
    description: "Popovers, dropdown menus, command palette."
    shadow: "0 8px 24px rgba(0, 0, 0, 0.45), 0 0 0 1px rgba(255, 255, 255, 0.04)"
    bg: "{colors.semantic.dark.surface.overlay}"
  "3":
    description: "Modal dialog, side drawer."
    shadow: "0 24px 64px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.06)"
    bg: "{colors.semantic.dark.surface.overlay}"

  # Light-mode shadows are softer and warmer.
  light_overrides:
    "1": { shadow: "0 1px 0 rgba(12, 13, 15, 0.06)" }
    "2": { shadow: "0 8px 20px rgba(12, 13, 15, 0.10), 0 0 0 1px rgba(12, 13, 15, 0.05)" }
    "3": { shadow: "0 24px 48px rgba(12, 13, 15, 0.15), 0 0 0 1px rgba(12, 13, 15, 0.08)" }

# -------------------------------------------------------------------- #
# 5. Shapes — radius scale already in layout. Stroke widths here.      #
# -------------------------------------------------------------------- #

shapes:
  stroke:
    hairline: "1px"             # default divider, card edge
    focus:    "2px"             # focus ring (offset outline)
    accent:   "2px"             # active inbox row left-edge marker
  focus_ring:
    style:   "solid"
    width:   "{shapes.stroke.focus}"
    offset:  "2px"
    color:   "{colors.semantic.dark.accent.ring}"
  motion:
    fast:    "120ms cubic-bezier(0.2, 0, 0, 1)"   # hover, focus, button press
    normal:  "200ms cubic-bezier(0.2, 0, 0, 1)"   # dialog enter/exit, pane swap
    slow:    "320ms cubic-bezier(0.2, 0, 0, 1)"   # reserved — toasts, drawers
    reduced: "0ms"                                # honored when prefers-reduced-motion

# -------------------------------------------------------------------- #
# 6. Components — token mappings. NO React code.                       #
# -------------------------------------------------------------------- #

components:
  button:
    base:
      font: "{typography.scale.body}"
      weight: 500
      padding_x: "{layout.spacing.3}"
      padding_y: "{layout.spacing.2}"
      radius: "{layout.radius.md}"
      transition: "{shapes.motion.fast}"
      focus_ring: "{shapes.focus_ring}"
    variants:
      primary:
        bg: "{colors.semantic.dark.accent.default}"
        bg_hover: "{colors.semantic.dark.accent.hover}"
        text: "{colors.semantic.dark.text.inverse}"
        border: "none"
      secondary:
        bg: "{colors.semantic.dark.surface.raised}"
        bg_hover: "{colors.semantic.dark.surface.overlay}"
        text: "{colors.semantic.dark.text.primary}"
        border: "1px solid {colors.semantic.dark.border.default}"
      ghost:
        bg: "transparent"
        bg_hover: "{colors.semantic.dark.surface.raised}"
        text: "{colors.semantic.dark.text.secondary}"
        text_hover: "{colors.semantic.dark.text.primary}"
        border: "none"
      danger:
        bg: "transparent"
        bg_hover: "{colors.semantic.dark.tier.red_tint}"
        text: "{colors.semantic.dark.tier.red}"
        border: "none"
      destructive_filled:           # used only in confirmation dialogs
        bg: "{colors.semantic.dark.tier.red}"
        text: "{colors.semantic.dark.text.inverse}"
        border: "none"
    sizes:
      sm: { padding_x: "{layout.spacing.2}", padding_y: "{layout.spacing.1}", font: "{typography.scale.body_sm}" }
      md: { padding_x: "{layout.spacing.3}", padding_y: "{layout.spacing.2}", font: "{typography.scale.body}" }
      lg: { padding_x: "{layout.spacing.4}", padding_y: "{layout.spacing.3}", font: "{typography.scale.body}" }

  input:
    bg: "{colors.semantic.dark.surface.raised}"
    bg_disabled: "{colors.semantic.dark.surface.sunken}"
    text: "{colors.semantic.dark.text.primary}"
    placeholder: "{colors.semantic.dark.text.muted}"
    border: "1px solid {colors.semantic.dark.border.default}"
    border_focus: "1px solid {colors.semantic.dark.accent.default}"
    border_invalid: "1px solid {colors.semantic.dark.tier.red}"
    radius: "{layout.radius.md}"
    padding_x: "{layout.spacing.3}"
    padding_y: "{layout.spacing.2}"
    font: "{typography.scale.body}"

  select:
    # Native <select>, themed. We do not roll a custom combobox in v1 —
    # native + Command-K palette for power use is the right combination.
    extends: "{components.input}"
    chevron: "{colors.semantic.dark.text.muted}"

  textarea:
    extends: "{components.input}"
    font: "{typography.scale.body_lg}"   # composers feel like writing tools
    line: "1.5rem"
    min_height: "6rem"
    measure: "{typography.measure.reading}"   # max-width: 72ch

  badge:
    base:
      font: "{typography.scale.mono_sm}"
      padding_x: "{layout.spacing.2}"
      padding_y: "0.125rem"
      radius: "{layout.radius.sm}"
      border: "1px solid"
    variants:
      tier_green:    { bg: "{colors.semantic.dark.tier.green_tint}",    text: "{colors.semantic.dark.tier.green}",    border_color: "{colors.semantic.dark.tier.green}" }
      tier_yellow:   { bg: "{colors.semantic.dark.tier.yellow_tint}",   text: "{colors.semantic.dark.tier.yellow}",   border_color: "{colors.semantic.dark.tier.yellow}" }
      tier_red:      { bg: "{colors.semantic.dark.tier.red_tint}",      text: "{colors.semantic.dark.tier.red}",      border_color: "{colors.semantic.dark.tier.red}" }
      status_open:        { bg: "{colors.semantic.dark.tier.green_tint}",  text: "{colors.semantic.dark.status.open}",     border_color: "transparent" }
      status_archiving:   { bg: "{colors.semantic.dark.tier.yellow_tint}", text: "{colors.semantic.dark.status.archiving}", border_color: "transparent" }
      status_archived:    { bg: "transparent",                              text: "{colors.semantic.dark.status.archived}",  border_color: "{colors.semantic.dark.border.subtle}" }
      status_abandoned:   { bg: "{colors.semantic.dark.tier.red_tint}",   text: "{colors.semantic.dark.status.abandoned}", border_color: "transparent" }
      status_blocked:     { bg: "{colors.semantic.dark.tier.yellow_tint}", text: "{colors.semantic.dark.status.blocked}",   border_color: "transparent" }
      status_escalated:   { bg: "{colors.semantic.dark.tier.red_tint}",   text: "{colors.semantic.dark.status.escalated}",  border_color: "transparent" }
      id_thread:          { bg: "transparent", text: "{colors.semantic.dark.id.thread}", border_color: "transparent", family: "mono" }
      id_task:            { bg: "transparent", text: "{colors.semantic.dark.id.task}",   border_color: "transparent", family: "mono" }

  agent_chip:
    # The agent name with a leading 6px role-colored dot. Used everywhere
    # an agent is rendered — message header, participant list, roster.
    dot_size: "6px"
    dot_radius: "{layout.radius.pill}"
    gap: "{layout.spacing.2}"
    font: "{typography.scale.body_sm}"
    color_manager: "{colors.semantic.dark.agent.manager}"
    color_worker:  "{colors.semantic.dark.agent.worker}"
    color_founder: "{colors.semantic.dark.agent.founder}"
    color_text:    "{colors.semantic.dark.text.primary}"

  kbd_chip:
    # The little keycaps in the HelpDrawer and inline hints.
    bg: "{colors.semantic.dark.surface.raised}"
    text: "{colors.semantic.dark.text.primary}"
    border: "1px solid {colors.semantic.dark.border.default}"
    radius: "{layout.radius.sm}"
    padding_x: "{layout.spacing.2}"
    padding_y: "1px"
    font: "{typography.scale.mono_sm}"
    box_shadow: "inset 0 -1px 0 rgba(0, 0, 0, 0.4)"   # subtle keycap depth

  topbar:
    height: "48px"
    bg: "{colors.semantic.dark.surface.sunken}"
    border_bottom: "1px solid {colors.semantic.dark.border.subtle}"
    padding_x: "{layout.spacing.4}"
    elevation: "{elevation.1}"

  statusbar:
    # Always-visible 24px ribbon at the bottom. Daemon-connection status,
    # active org slug, current SSE stream count, build version.
    height: "24px"
    bg: "{colors.semantic.dark.surface.sunken}"
    border_top: "1px solid {colors.semantic.dark.border.subtle}"
    padding_x: "{layout.spacing.4}"
    font: "{typography.scale.mono_sm}"
    text: "{colors.semantic.dark.text.muted}"

  inbox_row:
    height_comfortable: "{layout.density.comfortable.row_height}"
    height_compact:     "{layout.density.compact.row_height}"
    padding_x: "{layout.spacing.3}"
    padding_y: "{layout.spacing.2}"
    bg_rest: "transparent"
    bg_hover: "{colors.semantic.dark.surface.raised}"
    bg_active: "{colors.semantic.dark.accent.muted}"
    active_left_marker:
      width: "{shapes.stroke.accent}"
      color: "{colors.semantic.dark.accent.default}"
    text_subject: "{typography.scale.body_sm}"
    text_meta: "{typography.scale.caption}"
    text_id: "{typography.scale.mono_sm}"
    needs_you_dot:
      size: "6px"
      color: "{colors.semantic.dark.accent.default}"
      position: "leading"   # before the subject
      # Fires when the last message's `addressed_to` includes "founder"
      # OR "@all". @all is the common case because the Composer defaults
      # to it; without the @all rule, this signal would almost never fire.

  message_bubble:
    base:
      bg: "{colors.semantic.dark.surface.raised}"
      border: "1px solid {colors.semantic.dark.border.subtle}"
      radius: "{layout.radius.lg}"
      padding: "{layout.spacing.4}"
      max_width: "{typography.measure.reading}"
      font: "{typography.scale.body_lg}"
    variants:
      founder:
        bg: "{colors.semantic.dark.accent.muted}"
        border: "1px solid rgba(24, 148, 211, 0.30)"
      worker: {}
      manager: {}
      decline:
        bg: "{colors.semantic.dark.tier.red_tint}"
        border: "1px solid rgba(208, 74, 58, 0.30)"
        text: "{colors.semantic.dark.tier.red}"
      system:
        bg: "transparent"
        border: "1px dashed {colors.semantic.dark.border.subtle}"
        radius: "{layout.radius.pill}"
        padding_y: "{layout.spacing.1}"
        padding_x: "{layout.spacing.3}"
        font: "{typography.scale.caption}"
        align: "center"
        max_width: "fit-content"

  dialog:
    elevation: "{elevation.3}"
    max_width: "32rem"            # 512px — keep dialogs scannable, not page-wide
    radius: "{layout.radius.lg}"
    padding: "{layout.spacing.5}"
    header_font: "{typography.scale.h2}"
    body_font: "{typography.scale.body}"
    backdrop: "{colors.semantic.dark.surface.scrim}"

  drawer:
    # Right-side slide-out. Reserved for HelpDrawer in v1; designed so the
    # future "Agent detail" drawer (clicking an agent_chip) drops in.
    width: "20rem"                # 320px
    elevation: "{elevation.3}"
    bg: "{colors.semantic.dark.surface.overlay}"
    border_left: "1px solid {colors.semantic.dark.border.default}"
    padding: "{layout.spacing.5}"

  toast:
    elevation: "{elevation.2}"
    radius: "{layout.radius.md}"
    padding_x: "{layout.spacing.4}"
    padding_y: "{layout.spacing.3}"
    font: "{typography.scale.body_sm}"
    width: "22rem"
    position: "bottom-right"
    offset: "{layout.spacing.5}"
    variants:
      info:    { border_left: "3px solid {colors.semantic.dark.feedback.info}" }
      success: { border_left: "3px solid {colors.semantic.dark.feedback.success}" }
      warning: { border_left: "3px solid {colors.semantic.dark.feedback.warning}" }
      danger:  { border_left: "3px solid {colors.semantic.dark.feedback.danger}" }

  empty_state:
    padding_y: "{layout.spacing.7}"
    icon_size: "32px"
    icon_color: "{colors.semantic.dark.text.muted}"
    title_font: "{typography.scale.h3}"
    title_color: "{colors.semantic.dark.text.secondary}"
    body_font: "{typography.scale.body}"
    body_color: "{colors.semantic.dark.text.muted}"
    align: "center"
    max_width: "28rem"
---

# Overview

HappyRanch is a desk-tool used by one person, every working day, for years. The
visual identity is **calm editorial cockpit**: dense, monochrome-leaning,
with a single saturated cyan-blue accent (`signal_500`) used sparingly —
the way a broadcast control room uses one indicator color, or the way a
financial terminal earns its density. Public Sans gives long-form messages
a humanist body face that holds up at 15px, and JetBrains Mono lives in
every spot where a string is "machine-readable" — IDs, timestamps,
keyboard chips, completion-status tokens.

What this is **not**: a default dark dashboard. We deliberately reject
pure-black canvases, gradient hero panels, neon accent ramps, and the
generic Vercel/Linear/Notion drop-shadow card aesthetic. The product is a
flat, layered surface — depth is conveyed by surface tint, not by
elevation, except where elevation actually means "this is floating
above the page" (popovers, modals, drawers).

Why this matters for HappyRanch specifically: every screen carries org slug,
agent identity, IDs (THR-NNN / TASK-NNN), tier color, timestamps, status,
keyboard hints. If the chrome is loud, the signal disappears. The design
language is built to make the *data* the foreground and the *UI* the
parchment.

# Colors

See the frontmatter for the full palette. The opinion behind it:

- **Two inks, never black-and-white.** Canvas in dark mode is `ink_950`
  (`#0c0d0f`) — a warm near-black with a 3-degree blue cast. Canvas in
  light mode is `paper` (`#fdfdfb`) — a sub-perceptual yellow cast. Pure
  `#000` and `#fff` are reserved for nothing; they look cheap on calibrated
  displays.
- **Three surface tints per mode.** `canvas` < `sunken` < `raised` <
  `overlay`. The InboxList sits on `sunken`. The ThreadDetailPane sits on
  `canvas`. A focused MessageBubble sits on `raised`. A modal sits on
  `overlay`. Depth is read by tint difference, not shadow.
- **One accent, never two.** `signal_500` is the only chromatic blue in
  the system. It marks: the active inbox row, the focused input border,
  the founder's message bubble, primary button fills, the focus ring, and
  the daemon-connected dot in the statusbar. If we want a second
  "accent," we have done something wrong.
- **Tier palette is the green/yellow/red semantic system.** `tier.green
  / yellow / red` carry status meaning — healthy / warning / failed —
  consumed by StatusBadge, decline message bubbles, and error text.
  Never used to tint a whole row or background; that reads as alarmist.
  Use the tint variants (`*_tint`, 14% alpha) for badge fills.
- **Two agent identity colors.** Manager = warm violet
  (`violet_500`), worker = cool teal (`teal_500`). They prefix every agent
  name with a 6px dot. Founder uses the accent blue (`signal_400`), so the
  founder reads as a participant *in the system* rather than an external
  intruder. Deliberately decoupled from tier colors so a yellow-tier
  manager and a green-tier worker can still be visually distinguished.

# Typography

- **Public Sans** for sans, **JetBrains Mono** for mono. Both ship as
  open-source weights 400 / 500 / 600 / 700; we self-host the WOFF2 files
  in `web/public/fonts/` (~80KB after subsetting). System fallbacks named
  in the stack so the page never blanks on first paint.
- **Scale is purposeful, not modular.** `body` is 14px / 22px (1.57
  line-height) — Tailwind's `text-sm`. `body_lg` is 15px / 24px (1.6) —
  used only where a human is reading prose (message bodies, the composer
  textarea). The reading column is capped at **72ch** because a single
  founder is *reading* these messages, not scanning them like a chat app.
- **Label text is not all-caps by default.** Only the `overline` step is
  uppercase (with 0.08em tracking, never tighter). All-caps everywhere is
  the design-system equivalent of shouting; it is reserved for "INBOX",
  "ARCHIVED", "PARTICIPANTS" — column headers, not field labels.
- **Mono carries machine identity.** Every ID (`THR-1234`, `TASK-87`,
  `MEM-005`), every timestamp, every keycap, every token in the audit log
  uses `mono_sm` or `mono_md`. This is the visual answer to the question
  "is this string a label for a human or a handle for a machine?"

# Layout

- **8pt grid, with 4pt hairline.** The spacing scale is `0, 4, 8, 12, 16,
  24, 32, 48, 64`. 4px is reserved for badge insets and kbd chip padding —
  the spots where 8px would feel airy and wrong.
- **Two reference shells.** The threads page is a fixed two-column grid:
  340px inbox + 1fr detail, no gap, single 1px divider. Future
  dashboard-style screens (Tasks, Audit, Trends) use a 240px left nav +
  1fr canvas with a 24px gap — different rhythm because they're scanning
  surfaces, not focus surfaces.
- **The app shell has a statusbar.** A persistent 24px ribbon at the
  bottom of the viewport showing: daemon-connected dot, active org slug
  (mono), SSE stream count, build commit. It's small, it's mono, it is
  the desk-tool answer to a tab title — a single founder always knows
  *where they are* and *whether the engine is running*.
- **Two density modes.** Comfortable (default, 44px InboxRow) and Compact
  (32px InboxRow). Toggled per-user and remembered in `localStorage`.
  Density does not change message-body type sizes — the prose stays
  legible regardless.

# Elevation & Depth

This is a flat product. Surface tint, not drop shadow, conveys hierarchy.
Shadows exist only for *floating* elements:

| Level | Use | Visual |
|---|---|---|
| 0 | Resting cards, rows, message bubbles | No shadow; sits on parent surface |
| 1 | Header bands (TopBar, ThreadHeader) | Single 1px ink line below |
| 2 | Popovers, dropdowns, command palette | Soft 8/24 shadow + 1px halo |
| 3 | Modals, drawers, toasts | Stronger 24/64 shadow + scrim backdrop |

Reduced-motion users get instant transitions; everyone else gets a single
120ms fast easing for hover/focus and 200ms for dialog mount. We do not
animate state changes in the inbox — that's a chat app trick, and it
hides changes from a power user.

# Shapes

- **Radius scale: 0 / 3 / 5 / 8 / pill.** A 3px radius on badges and kbd
  chips is the editorial signature — most design systems go to 4 or 6, we
  go tighter. It reads as typographic furniture, not as Web 2.0 round.
- **Hairline strokes everywhere.** 1px borders. The only place we use a
  2px stroke is the active-inbox-row left marker and the focus ring (with
  2px offset).
- **One motion curve.** `cubic-bezier(0.2, 0, 0, 1)` — eased-out, fast
  acceleration, slow settle. Three durations: 120ms / 200ms / 320ms. We
  do not have a "bouncy" mode. This is an ops tool.

# Components

Token mappings live in the frontmatter under `components`. Brief notes
on intent for the founder-facing inventory:

- **Button** has five variants: `primary`, `secondary`, `ghost`,
  `danger` (text-only red, hover tint), `destructive_filled` (only in
  Archive/Abandon confirmation modals). The header action row in
  ThreadHeader uses `ghost`, never `secondary` — chrome should not
  compete with the message body.
- **Input** / **Select** / **Textarea** share a single ink-on-raised
  treatment. The composer Textarea uses `body_lg` (15px / 24px) and is
  measure-capped at 72ch.
- **Badge** has tier variants (green/yellow/red), status variants
  (open/archiving/archived/abandoned/blocked/escalated), and ID variants
  (thread/task — monospace, no fill, just colored text). One badge
  component, many semantic skins.
- **AgentChip** is the load-bearing identity primitive. 6px role-colored
  dot + name. It is the *only* place agent identity is rendered. Manager
  vs worker is always visible without color contrast as the only signal,
  because the AgentChip can show the role next to the name in screen-
  reader mode (`aria-label="content_writer, worker"`).
- **KbdChip** has a subtle inset bottom shadow so the keycap reads as a
  key, not a tag. Mono, 11px.
- **TopBar** + **Statusbar** sandwich the body. Both elevation-1 sunken
  bands. Org slug appears in both, monospace.
- **InboxRow** has a 2px accent-blue left-edge marker when active and a
  6px accent-blue dot prefixing the subject when the last message is
  addressed to the founder (the "needs you" signal).
- **MessageBubble** has four variants: founder (accent-tinted), agent
  (raised default), decline (red-tinted), system (pill-shaped, dashed
  border, centered, caption-sized).
- **Dialog** is 512px wide, never page-wide. The product has dialogs for
  *actions*, not for content; if a screen needs a wider surface, it's a
  page, not a dialog.
- **Drawer** is reserved for right-side panels: HelpDrawer today, future
  "Agent detail" sidecar from clicking an AgentChip.
- **ToastQueue** docks bottom-right. 22rem wide. Variants are
  border-left-colored, not background-tinted — toasts are interruptive
  enough already.
- **EmptyState** is centered, 28rem max, an icon at 32px, a 3-line title +
  body, optionally a primary button. The "no thread selected" pane is an
  EmptyState.

# Do's and Don'ts

**DO** prefix every agent name with the AgentChip role dot. The dot is
the single visual answer to "is this a manager or a worker?" — no other
chrome should encode role.

**DO** use mono for everything machine-identified: IDs, timestamps,
keycaps, audit-event names, completion tokens. The visual contrast of
mono-vs-sans is doing real work — it tells the founder which strings
they can paste into the CLI as-is.

**DO** keep tier color confined to small badges and inline text — status
pills, decline bubble borders, error strings. Tier is a status verdict;
bleeding it across whole rows or backgrounds makes every screen feel
alarmist.

**DO** show the org slug in two places at once: TopBar (large,
clickable, switcher) and Statusbar (small, mono, ambient). The Statusbar
copy is the source of truth when the TopBar is offscreen during a long
audit-log scroll.

**DO** use the accent color exactly four ways: focus ring, primary
button fill, active inbox row marker, founder message bubble tint.
Anything else gets `border.strong` or `text.secondary`.

**DON'T** color an entire row or background by tier. Tier belongs in
badges and inline text — full-bleed tier color makes neutral rows read
as warnings.

**DON'T** use drop shadows on resting cards, rows, or message bubbles. If
something is floating, it's a popover/modal/toast and gets elevation
2 or 3. Everything else uses surface tint.

**DON'T** introduce a second accent color. Cyan-blue is the only
chromatic UI color. If you want to encode a new state, find it in the
tier/status/feedback semantic tokens or propose a new semantic token —
do not invent a magenta because "we needed something different."

**DON'T** all-caps form labels. Only the `overline` step is uppercase,
used for section headers (INBOX, PARTICIPANTS, SHORTCUTS). Field labels
use the `label` step in title case.

**DON'T** animate inbox/list state changes. Power users notice movement
as a signal; a thread that silently re-orders during a 200ms slide is a
thread that disappears mid-scan. Updates land immediately; the focus and
scroll position persist.

**DON'T** put more than five items in the TopBar nav. Threads, Tasks,
KB, Audit, Agents — that's the locked set. Trends / Traces live as
sub-routes under Audit. If a sixth needs a top-level slot, we are
designing a different product.

**DON'T** use icon-only buttons for destructive actions. Archive and
Abandon are text buttons with a keyboard hint. Icon-only is fine for
"refresh" and "filter clear"; never for "destroy this thread."
