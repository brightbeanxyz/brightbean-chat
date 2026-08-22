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
import { useCallback, useEffect, useState } from "react";

import { fetchPicker } from "../../api/flows";
import type { BuilderEnv } from "../../env";
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

  const load = useCallback(
    async (append: boolean, at: string | null) => {
      setStatus("loading");
      try {
        const query = { q: term, kind, folder, platform: platform ?? "", cursor: at ?? "" };
        const payload = await fetchPicker(env, query);
        setAssets((current) => (append ? [...current, ...payload.results] : payload.results));
        setFolders(payload.folders);
        setCursor(payload.next_cursor);
        setStatus("idle");
      } catch {
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
    <div className="fb-subgroup" role="dialog" aria-label="Choose from the media library">
      <div className="flex flex-wrap gap-1 mb-2">
        <input
          type="search"
          className="form-input-styled flex-1"
          placeholder="Search the library"
          aria-label="Search the library"
          value={term}
          onChange={(event) => setTerm(event.target.value)}
        />
        {fixedKind ? null : (
          <select className="bb-select w-28" aria-label="Kind" value={kind} onChange={(event) => setKind(event.target.value)}>
            {KINDS.map((option) => (
              <option key={option} value={option}>
                {option === "" ? "Any kind" : option}
              </option>
            ))}
          </select>
        )}
        <select className="bb-select w-32" aria-label="Folder" value={folder} onChange={(event) => setFolder(event.target.value)}>
          <option value="">All folders</option>
          <option value={ROOT_FOLDER}>No folder</option>
          {folders.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.name}
            </option>
          ))}
        </select>
        <button type="button" className="btn-link text-xs" onClick={onClose}>
          Close
        </button>
      </div>

      {status === "error" ? <p className="fb-field-error">The media library could not be reached.</p> : null}
      {status !== "loading" && assets.length === 0 ? <p className="fb-empty">Nothing in the library matches.</p> : null}

      <div className="fb-asset-grid">
        {assets.map((asset) => (
          <button key={asset.id} type="button" className="fb-asset" onClick={() => onPick(asset)}>
            {asset.thumbnail_url ? (
              <img className="fb-asset-thumb" src={asset.thumbnail_url} alt={asset.alt_text || asset.filename} />
            ) : (
              <span className="fb-asset-thumb flex items-center justify-center fb-empty">{asset.kind}</span>
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
          Load more
        </button>
      ) : null}
    </div>
  );
}
