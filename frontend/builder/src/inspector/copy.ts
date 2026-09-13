/**
 * Human labels for schema properties.
 *
 * The artefact carries almost no `title` keywords, and its `description`s are
 * developer copy citing SPEC sections — useful as help text, wrong as a label.
 * So a property falls back through: schema `title`, this table, then a
 * humanised property name. A node type added later gets the humanised form,
 * which is serviceable; adding a line here is the optional polish.
 */

export const LABELS: Record<string, string> = {
  blocks: "Message blocks",
  // Its own line, because the humanised fallback spells the brand "Whatsapp".
  whatsapp_template: "WhatsApp template",
  buttons: "Buttons",
  quick_replies: "Quick replies",
  followup: "If nobody answers",
  retry_unmatched: "Retry if they reply something else",
  actions: "Actions",
  flow_id: "Flow",
  match: "Match",
  rules: "Rules",
  mode: "Mode",
  duration: "Duration",
  continue_window: "Sending window",
  paths: "Paths",
  sticky: "Remember the path per contact",
  method: "Method",
  url: "URL",
  headers: "Headers",
  body: "Body",
  timeout_s: "Timeout (seconds)",
  response_mappings: "Save the response into",
  fallback_handle_on_error: "Follow the error handle on failure",
  question: "Question",
  reply_type: "Expected reply",
  target: "Save into",
  retry: "Retry",
  timeout: "Timeout",
  text: "Text",
  media_url: "Media URL",
  subject: "Subject",
  html_body: "Body (HTML)",
  from_override: "From address",
  media_id: "Library asset",
  caption: "Caption",
  cards: "Cards",
  image: "Image",
  title: "Title",
  subtitle: "Subtitle",
  url_button: "Link button",
  label: "Label",
  action: "Action",
  enabled: "Enabled",
  delay: "Delay",
  unit: "Unit",
  max: "Maximum",
  invalid_text: "Message on an invalid reply",
  days: "Days",
  from: "From",
  to: "To",
  use_contact_timezone: "Use the contact's timezone",
  weight: "Weight (%)",
  name: "Name",
  value: "Value",
  json_path: "JSON path",
  target_type: "Save as",
  verb: "Action",
  tag: "Tag",
  field: "Field",
  sequence: "Sequence",
  member: "Member",
  member_ids: "Members",
  via: "Send via",
  source: "Source",
  op: "Operator",
  key: "Key",
  type: "Type",
  id: "Id",
  datetime: "Date and time",
  date: "Date",
};

/** `html_body` -> `Html body`, as a last resort. */
export function humanize(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function labelFor(name: string, title?: string): string {
  return title ?? LABELS[name] ?? humanize(name);
}

/** Tag copy for the discriminated unions a person actually picks from. */
export const VARIANT_LABELS: Record<string, string> = {
  text: "Text",
  image: "Image",
  audio: "Audio",
  video: "Video",
  file: "File",
  card: "Card",
  gallery: "Gallery",
  url: "Link",
  postback: "Continue the flow",
  duration: "Wait a fixed time",
  date: "Wait until a date",
  add_tag: "Add a tag",
  remove_tag: "Remove a tag",
  set_field: "Set a field",
  clear_field: "Clear a field",
  subscribe_sequence: "Subscribe to a sequence",
  unsubscribe_sequence: "Unsubscribe from a sequence",
  open_conversation: "Open the conversation",
  close_conversation: "Close the conversation",
  assign_conversation: "Assign the conversation",
  notify_members: "Notify members",
};

export function variantLabel(tag: string): string {
  return VARIANT_LABELS[tag] ?? ENUM_LABELS[tag] ?? humanize(tag);
}

/**
 * Enum values, as a reader should see them.
 *
 * Every `enum` in the artefact used to render its options verbatim, so a select
 * offered `system_field`, `has_no_value`, `any_word` and `in_app`. Those are
 * wire values; the schema is the contract and these are the words.
 *
 * Deliberately not merged into VARIANT_LABELS: that table names the branches of
 * a tagged union, which a reader picks a *shape* from ("Wait a fixed time"),
 * and this one names a value inside a field. Several keys would collide with
 * different right answers — `date` is "Wait until a date" as a delay's mode and
 * "A date" as an expected reply.
 *
 * Anything missing still falls through to `humanize`, which gives "System
 * field" rather than `system_field` — serviceable, and the reason this is
 * polish rather than a correctness fix.
 */
export const ENUM_LABELS: Record<string, string> = {
  // What a question saves into, and what it will accept.
  system_field: "A field every contact has",
  variable: "A value for this run only",
  text: "Any text",
  number: "A number",
  url: "A link",

  // Comparisons, in a condition.
  has: "has",
  has_not: "does not have",
  has_value: "has any value",
  no_value: "is empty",
  is: "is",
  is_not: "is not",
  in: "is one of",
  not_in: "is not one of",
  not: "is not",
  contains: "contains",
  before: "is before",
  after: "is after",
  on: "is on",
  subscribed: "is subscribed to",
  inside: "is open",
  outside: "is closed",

  // How many, and how long.
  all: "all of them",
  any: "any of them",
  minutes: "minutes",
  hours: "hours",
  days: "days",

  // Where a notification goes.
  in_app: "In the app",

  // Days of the week, so a sending window reads as one.
  mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun",

  // Contact fields, by the names the CRM uses for them.
  first_name: "First name",
  last_name: "Last name",
  created_at: "Added on",
  last_interaction_at: "Last heard from",
  locale: "Language",
  timezone: "Timezone",

  // Platforms, spelled the way their owners spell them.
  instagram: "Instagram",
  messenger: "Facebook Messenger",
  whatsapp: "WhatsApp",
  telegram: "Telegram",
  sms: "SMS",
};


/**
 * What one item of a list is called, for the button that appends one.
 *
 * The adder used to read "Add", with "Add to Buttons" as its accessible name —
 * so a screen reader was told what it added and a reader was not. These are the
 * visible half, and they name the *thing*, not the list: you add a button, not
 * a Buttons.
 *
 * A list with no entry here falls back to "Add to <list label>", which is
 * serviceable and still says what it adds. Adding a line here is the polish.
 */
export const ADD_ONE: Record<string, string> = {
  blocks: "Add a message part",
  buttons: "Add a button",
  quick_replies: "Add a quick reply",
  cards: "Add a card",
  actions: "Add an action",
  rules: "Add a rule",
  paths: "Add a path",
  headers: "Add a header",
  response_mappings: "Add a value to save",
  keywords: "Add a keyword",
  texts: "Add a version",
};

/**
 * How the "Add to this step" chips are grouped, and in what order.
 *
 * The headings answer "what is this for?" without opening anything, which a
 * flat row of four cannot: two of a send_message step's options are about
 * somebody going quiet and one is WhatsApp-only, and nothing said so.
 *
 * Keys, not node types, because the same property means the same thing
 * wherever it appears — `followup` is a wait on send_message and on
 * data_collection alike. A key in no group falls into the last one, so a
 * property added by a later layer is never silently dropped off the panel.
 */
export const ADD_GROUPS: readonly { label: string; keys: readonly string[] }[] = [
  { label: "Things they can tap", keys: ["buttons", "quick_replies"] },
  { label: "If they go quiet", keys: ["followup", "retry_unmatched", "retry", "timeout"] },
  { label: "WhatsApp only", keys: ["whatsapp_template", "template"] },
  { label: "More", keys: [] },
];
