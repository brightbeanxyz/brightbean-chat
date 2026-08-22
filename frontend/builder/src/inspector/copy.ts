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
  buttons: "Buttons",
  quick_replies: "Quick replies",
  followup: "Follow-up",
  retry_unmatched: "Retry on unrecognised reply",
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
  return VARIANT_LABELS[tag] ?? humanize(tag);
}
