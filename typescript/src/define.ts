/**
 * Schema definition — define node and value types in TypeScript.
 *
 * This is the TS equivalent of Python's @node decorator + atomdoc_schema().
 * Define your document schema, then export it for clients or use it
 * to create a local document.
 *
 * Usage:
 *
 *   const Color = defineValue("Color", {
 *     r: { type: "integer", default: 0 },
 *     g: { type: "integer", default: 0 },
 *     b: { type: "integer", default: 0 },
 *   }, { frozen: true });
 *
 *   const Annotation = defineNode("Annotation", {
 *     label: { type: "string", default: "" },
 *     color: { type: "object", schema: Color, tier: "atomic", default: { r: 0, g: 0, b: 0 } },
 *   });
 *
 *   const Page = defineNode("Page", {
 *     title: { type: "string", default: "" },
 *     // A reference to another node in the same document (stored as its ID)
 *     cover: { type: "ref", target: "Annotation", nullable: true, default: null },
 *   }, {
 *     slots: { annotations: "Annotation" },
 *   });
 *
 *   const schema = buildSchema("Page", [Page, Annotation], [Color]);
 */

import type { AtomDocSchema, HandleDef, NodeTypeDef, RefDef, ValueTypeDef } from "./types.js";

// ---------------------------------------------------------------------------
// Field definition
// ---------------------------------------------------------------------------

export type FieldType =
  | "string"
  | "integer"
  | "number"
  | "boolean"
  | "object"
  | "array"
  | "ref";

export interface FieldDef {
  type: FieldType;
  /** Default value for this field. */
  default?: unknown;
  /**
   * Tier: "mergeable" (default), "atomic", or "opaque". A `"ref"` field is
   * always tier "ref".
   */
  tier?: "mergeable" | "atomic" | "opaque" | "ref";
  /** For object fields: a ValueDef or NodeDef to reference. */
  schema?: ValueDef;
  /** For array fields: item type. */
  items?: FieldDef;
  /** For ref fields: the node type the reference must point at (null = any). */
  target?: string | null;
  /** For ref fields: the value is an array of node IDs. */
  many?: boolean;
  /**
   * The field also accepts `null` (Python `X | None`). Implied by
   * `default: null`; exported as `anyOf: [..., {type: "null"}]`.
   */
  nullable?: boolean;
  /**
   * For object fields whose value type is a handle: how much the document
   * depends on it. Exported in the node type's `handles` block.
   */
  strength?: "weak" | "strong";
}

// ---------------------------------------------------------------------------
// Value type definition (frozen compound types like Color)
// ---------------------------------------------------------------------------

export interface ValueDef {
  name: string;
  fields: Record<string, FieldDef>;
  frozen: boolean;
  /** Set when the value type is a handle to something outside the document. */
  handle?: { strength: "weak" | "strong" };
}

export function defineValue(
  name: string,
  fields: Record<string, FieldDef>,
  options: { frozen?: boolean; handle?: { strength: "weak" | "strong" } } = {},
): ValueDef {
  return {
    name,
    fields,
    frozen: options.frozen ?? true,
    ...(options.handle ? { handle: options.handle } : {}),
  };
}

/**
 * A handle value type: `uri` plus optional `media_type` and `digest`,
 * matching Python's `atomdoc.Handle`. Subclass-equivalent: pass a name and
 * the strength.
 */
export function defineHandle(
  name: string,
  strength: "weak" | "strong" = "weak",
  extraFields: Record<string, FieldDef> = {},
): ValueDef {
  return defineValue(
    name,
    {
      uri: { type: "string" },
      media_type: { type: "string", default: "" },
      digest: { type: "string", default: "" },
      ...extraFields,
    },
    { frozen: true, handle: { strength } },
  );
}

// ---------------------------------------------------------------------------
// Node type definition
// ---------------------------------------------------------------------------

export interface NodeDef {
  name: string;
  fields: Record<string, FieldDef>;
  slots: Record<string, string | null>;
}

export function defineNode(
  name: string,
  fields: Record<string, FieldDef>,
  options: { slots?: Record<string, string | null> } = {},
): NodeDef {
  return {
    name,
    fields,
    slots: options.slots ?? {},
  };
}

// ---------------------------------------------------------------------------
// Schema builder
// ---------------------------------------------------------------------------

function fieldToJsonSchema(field: FieldDef): Record<string, unknown> {
  const base = fieldToJsonSchemaBase(field);
  if (field.nullable || field.default === null) {
    const { default: def, ...rest } = base;
    const out: Record<string, unknown> = { anyOf: [rest, { type: "null" }] };
    if (def !== undefined) out.default = def;
    return out;
  }
  return base;
}

function fieldToJsonSchemaBase(field: FieldDef): Record<string, unknown> {
  if (field.type === "ref") {
    // A reference is a node ID on the wire.
    const result: Record<string, unknown> = field.many
      ? { type: "array", items: { type: "string" } }
      : { type: "string" };
    if (field.default !== undefined) result.default = field.default;
    return result;
  }

  const result: Record<string, unknown> = { type: field.type };

  if (field.default !== undefined) {
    result.default = field.default;
  }

  if (field.type === "object" && field.schema) {
    result.properties = {};
    for (const [k, f] of Object.entries(field.schema.fields)) {
      (result.properties as Record<string, unknown>)[k] = fieldToJsonSchema(f);
    }
  }

  if (field.type === "array" && field.items) {
    result.items = fieldToJsonSchema(field.items);
  }

  return result;
}

function nodeDefToTypeDef(node: NodeDef): NodeTypeDef {
  const properties: Record<string, Record<string, unknown>> = {};
  const fieldTiers: Record<string, string> = {};
  const fieldDefaults: Record<string, unknown> = {};
  const refs: Record<string, RefDef> = {};
  const handles: Record<string, HandleDef> = {};

  for (const [name, field] of Object.entries(node.fields)) {
    properties[name] = fieldToJsonSchema(field);
    if (field.type === "object" && field.schema?.handle) {
      handles[name] = {
        value_type: field.schema.name,
        strength: field.strength ?? field.schema.handle.strength,
      };
    }
    if (field.type === "ref") {
      fieldTiers[name] = "ref";
      refs[name] = {
        target_type: field.target ?? null,
        many: field.many ?? false,
        policy: "restrict",
      };
    } else {
      fieldTiers[name] = field.tier ?? "mergeable";
    }
    if (field.default !== undefined) {
      fieldDefaults[name] = field.default;
    }
  }

  const slots: Record<string, { allowed_type: string | null }> = {};
  for (const [name, allowedType] of Object.entries(node.slots)) {
    slots[name] = { allowed_type: allowedType };
  }

  return {
    json_schema: { type: "object", properties },
    field_tiers: fieldTiers,
    slots,
    field_defaults: fieldDefaults,
    refs,
    handles,
  };
}

function valueDefToTypeDef(value: ValueDef): ValueTypeDef {
  const properties: Record<string, Record<string, unknown>> = {};
  const required: string[] = [];
  for (const [name, field] of Object.entries(value.fields)) {
    properties[name] = fieldToJsonSchema(field);
    if (field.default === undefined) required.push(name);
  }

  return {
    json_schema: required.length > 0
      ? { type: "object", properties, required }
      : { type: "object", properties },
    frozen: value.frozen,
    ...(value.handle ? { handle: value.handle } : {}),
  };
}

/**
 * Build an AtomDocSchema from node and value type definitions.
 *
 * This produces the same schema format as Python's `doc.atomdoc_schema()`.
 */
export function buildSchema(
  rootType: string,
  nodes: NodeDef[],
  values: ValueDef[] = [],
): AtomDocSchema {
  const nodeTypes: Record<string, NodeTypeDef> = {};
  for (const node of nodes) {
    nodeTypes[node.name] = nodeDefToTypeDef(node);
  }

  const valueTypes: Record<string, ValueTypeDef> = {};
  for (const value of values) {
    valueTypes[value.name] = valueDefToTypeDef(value);
  }

  return {
    version: 1,
    root_type: rootType,
    node_types: nodeTypes,
    value_types: valueTypes,
  };
}
