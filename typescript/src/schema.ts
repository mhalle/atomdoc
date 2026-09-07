/**
 * Schema registry — hydrates the atomdoc schema into Zod validators.
 */

import { z } from "zod";
import type { AtomDocSchema, HandleDef, NodeTypeDef, RefDef, ValueTypeDef } from "./types.js";

export class SchemaRegistry {
  private nodeTypes: Map<string, NodeTypeDef>;
  private valueTypes: Map<string, ValueTypeDef>;
  private zodSchemas = new Map<string, z.ZodType>();

  constructor(schema: AtomDocSchema) {
    this.nodeTypes = new Map(Object.entries(schema.node_types));
    this.valueTypes = new Map(Object.entries(schema.value_types));
  }

  getNodeType(name: string): NodeTypeDef | undefined {
    return this.nodeTypes.get(name);
  }

  getValueType(name: string): ValueTypeDef | undefined {
    return this.valueTypes.get(name);
  }

  getFieldTier(
    nodeType: string,
    field: string,
  ): string | undefined {
    return this.nodeTypes.get(nodeType)?.field_tiers[field];
  }

  getSlots(
    nodeType: string,
  ): Record<string, { allowed_type: string | null }> {
    return this.nodeTypes.get(nodeType)?.slots ?? {};
  }

  /** Reference fields of a node type (tier "ref"), keyed by field name. */
  getRefs(nodeType: string): Record<string, RefDef> {
    return this.nodeTypes.get(nodeType)?.refs ?? {};
  }

  /** The reference declaration of one field, if it is a ref. */
  getRef(nodeType: string, field: string): RefDef | undefined {
    return this.nodeTypes.get(nodeType)?.refs?.[field];
  }

  /** Handle fields of a node type, keyed by field name. */
  getHandles(nodeType: string): Record<string, HandleDef> {
    return this.nodeTypes.get(nodeType)?.handles ?? {};
  }

  getDefaults(nodeType: string): Record<string, unknown> {
    return this.nodeTypes.get(nodeType)?.field_defaults ?? {};
  }

  /** Get or build a Zod schema for a node or value type. */
  getZodSchema(typeName: string): z.ZodType | undefined {
    const cached = this.zodSchemas.get(typeName);
    if (cached) return cached;

    const nodeDef = this.nodeTypes.get(typeName);
    if (nodeDef) {
      const schema = jsonSchemaToZod(nodeDef.json_schema);
      this.zodSchemas.set(typeName, schema);
      return schema;
    }

    const valueDef = this.valueTypes.get(typeName);
    if (valueDef) {
      const schema = jsonSchemaToZod(valueDef.json_schema);
      const frozen = valueDef.frozen ? schema.readonly() : schema;
      this.zodSchemas.set(typeName, frozen);
      return frozen;
    }

    return undefined;
  }

  /** Validate data against a named type's schema. */
  validate(typeName: string, data: unknown): unknown {
    const schema = this.getZodSchema(typeName);
    if (!schema) {
      throw new Error(`Unknown type: ${typeName}`);
    }
    return schema.parse(data);
  }

  /** List all node type names. */
  nodeTypeNames(): string[] {
    return [...this.nodeTypes.keys()];
  }

  /** List all value type names. */
  valueTypeNames(): string[] {
    return [...this.valueTypes.keys()];
  }
}

/**
 * Convert a JSON Schema object (the subset atomdoc exports) to a Zod schema.
 *
 * Supports: string/integer/number/boolean/null, `enum`, `const`, string
 * length and pattern constraints, numeric bounds, arrays (with `items`,
 * `prefixItems`, `minItems`, `maxItems`), objects (with `properties`,
 * `required`, `additionalProperties`), `anyOf`/`oneOf` unions (with a
 * `discriminator` when every variant carries a literal for it), and
 * `default`. A `null` default makes the schema nullable. An object without
 * a `required` list keeps the earlier rule: a property is required unless
 * it has a default.
 */
function jsonSchemaToZod(jsonSchema: Record<string, unknown>): z.ZodType {
  return withDefault(jsonSchema, buildZod(jsonSchema));
}

function withDefault(jsonSchema: Record<string, unknown>, schema: z.ZodType): z.ZodType {
  if (!("default" in jsonSchema)) return schema;
  const def = jsonSchema.default;
  let out = schema;
  if (def === null && !schema.isNullable()) out = out.nullable();
  return out.default(def as never);
}

function literalOf(value: unknown): z.ZodType {
  if (value === null) return z.null();
  return z.literal(value as string | number | boolean);
}

function buildZod(jsonSchema: Record<string, unknown>): z.ZodType {
  if ("const" in jsonSchema) return literalOf(jsonSchema.const);
  if (Array.isArray(jsonSchema.enum)) {
    const values = jsonSchema.enum as unknown[];
    if (values.length === 1) return literalOf(values[0]);
    if (values.every((v) => typeof v === "string")) {
      return z.enum(values as [string, ...string[]]);
    }
    const lits = values.map(literalOf);
    return z.union(lits as [z.ZodType, z.ZodType, ...z.ZodType[]]);
  }

  const variants = (jsonSchema.oneOf ?? jsonSchema.anyOf) as
    | Record<string, unknown>[]
    | undefined;
  if (Array.isArray(variants) && variants.length > 0) {
    const options = variants.map(jsonSchemaToZod);
    if (options.length === 1) return options[0];
    const disc = jsonSchema.discriminator as { propertyName?: string } | undefined;
    const key = disc?.propertyName;
    const allTagged = key !== undefined && options.every(
      (o) => o instanceof z.ZodObject && isLiteral(o.shape[key]),
    );
    if (allTagged) {
      return z.discriminatedUnion(
        key as string,
        options as unknown as [
          z.ZodDiscriminatedUnionOption<string>,
          ...z.ZodDiscriminatedUnionOption<string>[],
        ],
      );
    }
    return z.union(options as [z.ZodType, z.ZodType, ...z.ZodType[]]);
  }

  const allOf = jsonSchema.allOf as Record<string, unknown>[] | undefined;
  if (Array.isArray(allOf) && allOf.length > 0) {
    const parts = allOf.map(buildZod);
    let combined: z.ZodType = parts[0];
    for (const part of parts.slice(1)) combined = z.intersection(combined, part);
    return combined;
  }

  const rawType = jsonSchema.type;
  if (Array.isArray(rawType)) {
    // `"type": ["string", "null"]`: one of several primitive types.
    const options = rawType.map((t) => buildZod({ ...jsonSchema, type: t }));
    if (options.length === 1) return options[0];
    return z.union(options as [z.ZodType, z.ZodType, ...z.ZodType[]]);
  }
  const type = rawType as string | undefined;

  if (type === "null") return z.null();
  if (type === "string") {
    let str = z.string();
    if (typeof jsonSchema.minLength === "number") str = str.min(jsonSchema.minLength);
    if (typeof jsonSchema.maxLength === "number") str = str.max(jsonSchema.maxLength);
    if (typeof jsonSchema.pattern === "string") {
      const re = pythonRegexToJs(jsonSchema.pattern);
      if (re) str = str.regex(re);
    }
    return str;
  }
  if (type === "integer" || type === "number") {
    let num = z.number();
    if (type === "integer") num = num.int();
    if (typeof jsonSchema.minimum === "number") num = num.min(jsonSchema.minimum);
    if (typeof jsonSchema.maximum === "number") num = num.max(jsonSchema.maximum);
    if (typeof jsonSchema.exclusiveMinimum === "number") num = num.gt(jsonSchema.exclusiveMinimum);
    if (typeof jsonSchema.exclusiveMaximum === "number") num = num.lt(jsonSchema.exclusiveMaximum);
    return num;
  }
  if (type === "boolean") return z.boolean();
  if (type === "array") {
    const prefix = jsonSchema.prefixItems as Record<string, unknown>[] | undefined;
    if (Array.isArray(prefix) && prefix.length > 0) {
      const tuple = z.tuple(prefix.map(jsonSchemaToZod) as [] | [z.ZodType, ...z.ZodType[]]);
      return jsonSchema.items && typeof jsonSchema.items === "object"
        ? tuple.rest(jsonSchemaToZod(jsonSchema.items as Record<string, unknown>))
        : tuple;
    }
    const items = (jsonSchema.items ?? {}) as Record<string, unknown>;
    let arr = z.array(jsonSchemaToZod(items));
    if (typeof jsonSchema.minItems === "number") arr = arr.min(jsonSchema.minItems);
    if (typeof jsonSchema.maxItems === "number") arr = arr.max(jsonSchema.maxItems);
    return arr;
  }
  if (type === "object" || jsonSchema.properties) {
    const properties = (jsonSchema.properties ?? {}) as Record<
      string,
      Record<string, unknown>
    >;
    const additional = jsonSchema.additionalProperties;
    if (Object.keys(properties).length === 0 && additional && typeof additional === "object") {
      // A mapping: Python `dict[str, T]`.
      return z.record(jsonSchemaToZod(additional as Record<string, unknown>));
    }
    const required = Array.isArray(jsonSchema.required)
      ? new Set(jsonSchema.required as string[])
      : null;
    const shape: Record<string, z.ZodType> = {};
    for (const [key, propSchema] of Object.entries(properties)) {
      let prop = jsonSchemaToZod(propSchema);
      if (required !== null && !required.has(key) && !("default" in propSchema)) {
        prop = prop.optional();
      }
      shape[key] = prop;
    }
    const obj = z.object(shape);
    if (additional === false) return obj.strict();
    if (additional && typeof additional === "object") {
      return obj.catchall(jsonSchemaToZod(additional as Record<string, unknown>));
    }
    return obj.passthrough();
  }

  // Fallback: accept anything
  return z.unknown();
}

function isLiteral(schema: z.ZodType | undefined): boolean {
  if (!schema) return false;
  if (schema instanceof z.ZodLiteral) return true;
  if (schema instanceof z.ZodDefault || schema instanceof z.ZodOptional) {
    return isLiteral((schema._def as { innerType: z.ZodType }).innerType);
  }
  return false;
}

/**
 * Translate a Python (Rust `regex` crate) pattern to a JS RegExp. Named
 * groups and the string anchors differ; inline flags are not supported.
 * Returns null when the pattern cannot be compiled, in which case the
 * string is left unconstrained rather than rejecting every value.
 */
function pythonRegexToJs(pattern: string): RegExp | null {
  const translated = pattern
    .replace(/\(\?P</g, "(?<")
    .replace(/\\A/g, "^")
    .replace(/\\[Zz]/g, "$");
  try {
    return new RegExp(translated, "u");
  } catch {
    try {
      return new RegExp(translated);
    } catch {
      return null;
    }
  }
}
