/**
 * Wire protocol types for the AtomDoc client.
 *
 * These types mirror the Python server's protocol exactly.
 * The "0" sentinel represents null/root in operation tuples.
 */

// ---------------------------------------------------------------------------
// Node store
// ---------------------------------------------------------------------------

/** A node as stored in the client-side flat map. */
export interface StoreNode {
  id: string;
  type: string;
  state: Record<string, unknown>;
  /** slot name → ordered child IDs */
  slots: Record<string, string[]>;
  parentId: string | null;
  slotName: string | null;
}

// ---------------------------------------------------------------------------
// Wire operations (match Python _types.py)
// ---------------------------------------------------------------------------

/** Insert: [0, [[id, type], ...], parentId|0, slotName, prevId|0, nextId|0] */
export type InsertOp = [
  0,
  [string, string][],
  string | 0,
  string,
  string | 0,
  string | 0,
];

/** Delete: [1, startId, endId|0] */
export type DeleteOp = [1, string, string | 0];

/** Move: [2, startId, endId|0, parentId|0, slotName, prevId|0, nextId|0] */
export type MoveOp = [
  2,
  string,
  string | 0,
  string | 0,
  string,
  string | 0,
  string | 0,
];

export type OrderedOp = InsertOp | DeleteOp | MoveOp;

export interface WireOperations {
  ordered: OrderedOp[];
  /**
   * State patches — native JSON values per field.
   * Opaque/bytes fields are base64-encoded strings; the receiver decodes
   * based on the field's schema tier.
   */
  state: Record<string, Record<string, unknown>>;
}

// ---------------------------------------------------------------------------
// Protocol messages — server to client
// ---------------------------------------------------------------------------

export interface SchemaMsg {
  type: "schema";
  schema: AtomDocSchema;
}

export interface SnapshotMsg {
  type: "snapshot";
  doc_id: string;
  version: number;
  data: JsonDoc;
  client_id?: string;
}

export interface PatchMsg {
  type: "patch";
  version: number;
  operations: WireOperations;
  /**
   * Set only when the patch is the verbatim echo of that client's `op`;
   * null for a `create`/`undo`/`redo` result, a normalized commit, or a
   * host-side change.
   */
  source_client: string | null;
  /** The `ref` of the client request that produced this patch, if any. */
  ref?: string | null;
}

export interface ErrorMsg {
  type: "error";
  ref?: string;
  code: string;
  message: string;
}

export type ServerMsg = SchemaMsg | SnapshotMsg | PatchMsg | ErrorMsg;

// ---------------------------------------------------------------------------
// Protocol messages — client to server
// ---------------------------------------------------------------------------

export interface OpMsg {
  type: "op";
  ref?: string;
  operations: WireOperations;
}

export interface CreateMsg {
  type: "create";
  ref?: string;
  node_type: string;
  state: Record<string, unknown>;
  parent_id?: string;
  slot: string;
  position?: string;
  target_id?: string;
}

export interface UndoMsg {
  type: "undo";
  ref?: string;
  steps?: number;
}

export interface RedoMsg {
  type: "redo";
  ref?: string;
  steps?: number;
}

export type ClientMsg = OpMsg | CreateMsg | UndoMsg | RedoMsg;

// ---------------------------------------------------------------------------
// Schema types (from atomdoc_schema())
// ---------------------------------------------------------------------------

/**
 * A reference field: the state value is the target node's ID (or an array
 * of IDs when `many`). References are association, not ownership — the
 * target lives in some slot and is never deleted through the reference.
 */
export interface RefDef {
  /** Node type the reference must point at, or null for any node. */
  target_type: string | null;
  /** `list[Ref[T]]` in Python: the value is an array of IDs. */
  many: boolean;
  /** Delete policy. `"restrict"`: a referenced node cannot be deleted. */
  policy: "restrict";
}

/**
 * A handle field: a frozen value naming something outside the document
 * (bulk data, another document, an ontology term). `strength` says whether
 * the document is usable without resolving it ("weak") or not ("strong").
 */
export interface HandleDef {
  /** Name of the value type (a `Handle` subclass in Python). */
  value_type: string;
  strength: "weak" | "strong";
}

export interface NodeTypeDef {
  json_schema: Record<string, unknown>;
  /** Field name → "mergeable" | "atomic" | "opaque" | "ref". */
  field_tiers: Record<string, string>;
  slots: Record<string, { allowed_type: string | null }>;
  field_defaults: Record<string, unknown>;
  /** Reference fields (tier "ref"), keyed by field name. */
  refs?: Record<string, RefDef>;
  /** Handle fields, keyed by field name. */
  handles?: Record<string, HandleDef>;
}

export interface ValueTypeDef {
  json_schema: Record<string, unknown>;
  frozen: boolean;
  /** Present when the value type is a handle. */
  handle?: { strength: "weak" | "strong" };
}

export interface AtomDocSchema {
  version: number;
  root_type: string;
  node_types: Record<string, NodeTypeDef>;
  value_types: Record<string, ValueTypeDef>;
}

// ---------------------------------------------------------------------------
// Snapshot wire format
// ---------------------------------------------------------------------------

/** [id, type, {state}, {slotName: [children...]}?] */
export type JsonDoc = [
  string,
  string,
  Record<string, unknown>,
  Record<string, JsonDoc[]>?,
];
