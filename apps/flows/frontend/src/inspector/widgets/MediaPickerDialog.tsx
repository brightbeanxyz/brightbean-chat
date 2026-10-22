/**
 * The media library picker, against apps/media_library/picker.py's contract.
 *
 * Its docstring *is* the contract, and two of its consumer notes are the whole
 * design here:
 *
 * * "Store `id`. Never store `url` — it is minted per request." So choosing an
 *   asset writes `media_id`, and the delivery URL is used only to draw the
 *   thumbnail in this dialog.
 * * "`platform_warnings` … never means 'cannot attach'". They are rendered
 *   beside the asset as advice, and never disable it.
 *
 * Paging is keyset: pass the previous `next_cursor` back and do not parse it.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { fetchPicker } from "../../api/flows";
import type { BuilderEnv } from "../../env";
import i18n from "../../i18n";
import { variantLabel } from "../copy";
import type { MediaAsset, MediaFolder } from "../../schema/types";

const KINDS = ["", "image", "audio", "video", "file"] as const;

/** `root` is the picker's value for "assets in no folder"; "" means all. */
const ROOT_FOLDER = "root";

export interface MediaPickerDialogProps {
  env: BuilderEnv;
  /** Populates `platform_warnings`; advisory, and never filters. */
  platform?: string;
  kind?: string;
  onPick: (asset: MediaAsset) => void;
  onClose: () => void;
}

export function MediaPickerDialog({ env, platform, kind: fixedKind, onPick, onClose }: MediaPickerDialogProps) {
  const [term, setTerm] = useState("");
  const [kind, setKind] = useState(fixedKind ?? "");
  const [folder, setFolder] = useState("");
  const [assets, setAssets] = useState<MediaAsset[]>([]);
  const [folders, setFolders] = useState<MediaFolder[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "error">("idle");

  /**
   * Which request is the current one.
   *
   * Typing a search term starts a request per keystroke-burst, and nothing
   * guarantees they finish in order. Without this stamp an older response
   * overwrites the newer one, and the dialog shows — and lets you pick from —
   * results for a query that is no longer on screen.
   */
  const latest = useRef(0);

  const load = useCallback(
    async (append: boolean, at: string | null) => {
      const ticket = (latest.current += 1);
      setStatus("loading");
      try {
        const query = { q: term, kind, folder, platform: platform ?? "", cursor: at ?? "" };
        const payload = await fetchPicker(env, query);
        if (ticket !== latest.current) {
          return;
        }
        setAssets((current) => (append ? [...current, ...payload.results] : payload.results));
        setFolders(payload.folders);
        setCursor(payload.next_cursor);
        setStatus("idle");
      } catch {
        if (ticket !== latest.current) {
          return;
        }
        // A folder id this workspace cannot see answers 404 — a stale id, so
        // start again at the top rather than showing an error the user cannot
        // act on.
        if (folder) {
          setFolder("");
          return;
        }
        setStatus("error");
      }
    },
    [env, term, kind, folder, platform],
  );

  // Debounced so typing a search term is one request, not one per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => void load(false, null), 250);
    return () => clearTimeout(timer);
  }, [load]);

  return (
    <div className="fb-subgroup" role="dialog" aria-label={i18n.t("mediaPicker.dialogLabel")}>
      <div className="flex flex-wrap gap-1 mb-2">
        <input
          type="search"
          className="form-input-styled flex-1"
          placeholder={i18n.t("mediaPicker.searchPlaceholder")}
          aria-label={i18n.t("mediaPicker.searchLabel")}
          value={term}
          onChange={(event) => setTerm(event.target.value)}
        />
        {fixedKind ? null : (
          <span className="bb-select-wrap w-28">
            <select
              className="bb-select"
              aria-label={i18n.t("mediaPicker.kindLabel")}
              value={kind}
              onChange={(event) => setKind(event.target.value)}
            >
              {KINDS.map((option) => (
                <option key={option} value={option}>
                  {option === "" ? i18n.t("mediaPicker.anyKind") : variantLabel(option)}
                </option>
              ))}
            </select>
          </span>
        )}
        <span className="bb-select-wrap w-32">
          <select
            className="bb-select"
            aria-label={i18n.t("mediaPicker.folderLabel")}
            value={folder}
            onChange={(event) => setFolder(event.target.value)}
          >
            <option value="">{i18n.t("mediaPicker.allFolders")}</option>
            <option value={ROOT_FOLDER}>{i18n.t("mediaPicker.noFolder")}</option>
            {folders.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.name}
              </option>
            ))}
          </select>
        </span>
        <button type="button" className="btn-link text-xs" onClick={onClose}>
          {i18n.t("mediaPicker.close")}
        </button>
      </div>

      {status === "error" ? <p className="fb-field-error">{i18n.t("mediaPicker.loadFailed")}</p> : null}
      {status !== "loading" && assets.length === 0 ? (
        <p className="fb-empty">{i18n.t("mediaPicker.noMatches")}</p>
      ) : null}

      <div className="fb-asset-grid">
        {assets.map((asset) => (
          <button key={asset.id} type="button" className="fb-asset" onClick={() => onPick(asset)}>
            {asset.thumbnail_url ? (
              <img className="fb-asset-thumb" src={asset.thumbnail_url} alt={asset.alt_text || asset.filename} />
            ) : (
              <span className="fb-asset-thumb flex items-center justify-center fb-empty">
                {variantLabel(asset.kind)}
              </span>
            )}
            <span className="text-xs truncate">{asset.title || asset.filename}</span>
            {asset.platform_warnings.map((warning, index) => (
              <span key={index} className="fb-badge fb-badge-warning">
                {warning}
              </span>
            ))}
          </button>
        ))}
      </div>

      {cursor ? (
        <button type="button" className="btn-outline-sm mt-2" onClick={() => void load(true, cursor)}>
          {i18n.t("mediaPicker.loadMore")}
        </button>
      ) : null}
    </div>
  );
}
