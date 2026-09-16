/**
 * A validator for the JSON Schema subset atomdoc exports.
 *
 * The schema is authored on the server (pydantic today, `z.toJSONSchema()`
 * from a TypeScript server tomorrow) and reaches a client as JSON Schema, so
 * the client only ever *reads* schemas. That is a small enough job to do
 * here: a validation library in the client costs 58 KB (zod 3) to 442 KB
 * (zod 4 imported as a namespace), which is paid twice over in an MCP Apps
 * widget, where the module is inlined into a page the host caches per
 * connector. The server stays authoritative and re-validates every write.
 */

export class ValidationError extends Error {
  readonly path: string;

  constructor(message: string, path = "") {
    super(path ? `${message} at ${path}` : message);
    this.name = "ValidationError";
    this.path = path;
  }
}

/** A compiled schema. `fields` is set for objects, so one field can be checked alone. */
export interface Validator {
  parse(value: unknown): unknown;
  readonly fields?: Record<string, Validator>;
  readonly nullable?: boolean;
}

const at = (path: string, key: string | number): string =>
  typeof key === "number" ? `${path}[${key}]` : path ? `${path}.${key}` : key;

const typeName = (v: unknown): string =>
  v === null ? "null" : Array.isArray(v) ? "array" : typeof v;

function check(fn: (value: unknown, path: string) => unknown, extra: Partial<Validator> = {}): Validator {
  return { parse: (value: unknown) => fn(value, ""), ...extra, _walk: fn } as Validator;
}

/** The internal form: every validator carries a path-aware walk. */
type Walk = (value: unknown, path: string) => unknown;
const walkOf = (v: Validator): Walk => (v as unknown as { _walk: Walk })._walk;

/**
 * Translate a Python (Rust `regex` crate) pattern to a JS RegExp. Named groups
 * and the string anchors differ; inline flags are not supported. Returns null
 * when the pattern cannot be compiled, in which case the string is left
 * unconstrained rather than rejecting every value.
 */
export function pythonRegexToJs(pattern: string): RegExp | null {
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

/** Compile a JSON Schema object into a validator. */
export function compile(jsonSchema: Record<string, unknown>): Validator {
  return withDefault(jsonSchema, build(jsonSchema));
}

function withDefault(jsonSchema: Record<string, unknown>, inner: Validator): Validator {
  if (!("default" in jsonSchema)) return inner;
  const def = jsonSchema.default;
  const nullable = def === null || inner.nullable === true;
  const walk = walkOf(inner);
  return check((value, path) => {
    if (value === undefined) return clone(def);
    if (value === null && nullable) return null;
    return walk(value, path);
  }, { fields: inner.fields, nullable });
}

const clone = (v: unknown): unknown =>
  v === null || typeof v !== "object" ? v : structuredClone(v);

function literalOf(expected: unknown): Validator {
  return check((value, path) => {
    if (value !== expected) {
      throw new ValidationError(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(value)}`, path);
    }
    return value;
  }, { nullable: expected === null });
}

function union(options: Validator[]): Validator {
  if (options.length === 1) return options[0];
  const walks = options.map(walkOf);
  return check((value, path) => {
    const problems: string[] = [];
    for (const walk of walks) {
      try {
        return walk(value, path);
      } catch (e) {
        problems.push(e instanceof Error ? e.message : String(e));
      }
    }
    throw new ValidationError(`no variant matched (${problems.join("; ")})`, path);
  }, { nullable: options.some((o) => o.nullable) });
}

function taggedVariants(
  key: string,
  variants: Record<string, unknown>[],
): Map<unknown, Validator> | null {
  const byTag = new Map<unknown, Validator>();
  for (const variant of variants) {
    const properties = variant.properties as Record<string, Record<string, unknown>> | undefined;
    const prop = properties?.[key];
    if (!prop) return null;
    const values = "const" in prop ? [prop.const]
      : Array.isArray(prop.enum) && prop.enum.length === 1 ? [prop.enum[0]] : null;
    if (!values) return null;                       // not every variant carries a literal tag
    byTag.set(values[0], compile(variant));
  }
  return byTag;
}

function build(jsonSchema: Record<string, unknown>): Validator {
  if ("const" in jsonSchema) return literalOf(jsonSchema.const);

  if (Array.isArray(jsonSchema.enum)) {
    const values = jsonSchema.enum as unknown[];
    if (values.length === 1) return literalOf(values[0]);
    return check((value, path) => {
      if (!values.includes(value)) {
        throw new ValidationError(`expected one of ${JSON.stringify(values)}, got ${JSON.stringify(value)}`, path);
      }
      return value;
    }, { nullable: values.includes(null) });
  }

  const variants = (jsonSchema.oneOf ?? jsonSchema.anyOf) as Record<string, unknown>[] | undefined;
  if (Array.isArray(variants) && variants.length > 0) {
    const key = (jsonSchema.discriminator as { propertyName?: string } | undefined)?.propertyName;
    const tagged = key === undefined ? null : taggedVariants(key, variants);
    // A declared discriminator is pydantic's tagged union, and it reads the tag from the
    // data: a value without it is `union_tag_not_found` on the server, not a default.
    if (tagged) {
      return check((value, path) => {
        if (!isPlain(value)) throw new ValidationError(`expected object, got ${typeName(value)}`, path);
        const tag = (value as Record<string, unknown>)[key as string];
        if (tag === undefined) throw new ValidationError(`missing '${key}'`, path);
        const variant = tagged.get(tag);
        if (!variant) throw new ValidationError(`no variant for ${key}=${JSON.stringify(tag)}`, path);
        return walkOf(variant)(value, path);
      });
    }
    return union(variants.map(compile));
  }

  const allOf = jsonSchema.allOf as Record<string, unknown>[] | undefined;
  if (Array.isArray(allOf) && allOf.length > 0) {
    const parts = allOf.map(build).map(walkOf);
    return check((value, path) => {
      let out: unknown = value;
      for (const walk of parts) {
        const result = walk(value, path);
        out = isPlain(out) && isPlain(result) ? { ...(out as object), ...(result as object) } : result;
      }
      return out;
    });
  }

  const rawType = jsonSchema.type;
  if (Array.isArray(rawType)) {
    return union(rawType.map((t) => build({ ...jsonSchema, type: t })));
  }
  const type = rawType as string | undefined;

  if (type === "null") return literalOf(null);

  if (type === "string") {
    const min = num(jsonSchema.minLength), max = num(jsonSchema.maxLength);
    const re = typeof jsonSchema.pattern === "string" ? pythonRegexToJs(jsonSchema.pattern) : null;
    return check((value, path) => {
      if (typeof value !== "string") throw new ValidationError(`expected string, got ${typeName(value)}`, path);
      if (min !== undefined && value.length < min) throw new ValidationError(`shorter than ${min}`, path);
      if (max !== undefined && value.length > max) throw new ValidationError(`longer than ${max}`, path);
      if (re && !re.test(value)) throw new ValidationError(`does not match ${re}`, path);
      return value;
    });
  }

  if (type === "integer" || type === "number") {
    const int = type === "integer";
    const min = num(jsonSchema.minimum), max = num(jsonSchema.maximum);
    const gt = num(jsonSchema.exclusiveMinimum), lt = num(jsonSchema.exclusiveMaximum);
    return check((value, path) => {
      if (typeof value !== "number" || Number.isNaN(value)) {
        throw new ValidationError(`expected ${type}, got ${typeName(value)}`, path);
      }
      if (int && !Number.isInteger(value)) throw new ValidationError("expected an integer", path);
      if (min !== undefined && value < min) throw new ValidationError(`less than ${min}`, path);
      if (max !== undefined && value > max) throw new ValidationError(`greater than ${max}`, path);
      if (gt !== undefined && value <= gt) throw new ValidationError(`not greater than ${gt}`, path);
      if (lt !== undefined && value >= lt) throw new ValidationError(`not less than ${lt}`, path);
      return value;
    });
  }

  if (type === "boolean") {
    return check((value, path) => {
      if (typeof value !== "boolean") throw new ValidationError(`expected boolean, got ${typeName(value)}`, path);
      return value;
    });
  }

  if (type === "array") {
    const prefix = jsonSchema.prefixItems as Record<string, unknown>[] | undefined;
    const rest = jsonSchema.items && typeof jsonSchema.items === "object"
      ? walkOf(compile(jsonSchema.items as Record<string, unknown>)) : null;
    if (Array.isArray(prefix) && prefix.length > 0) {
      const heads = prefix.map(compile).map(walkOf);
      return check((value, path) => {
        if (!Array.isArray(value)) throw new ValidationError(`expected array, got ${typeName(value)}`, path);
        if (value.length < heads.length || (!rest && value.length > heads.length)) {
          throw new ValidationError(`expected ${heads.length} items, got ${value.length}`, path);
        }
        return value.map((item, i) => (i < heads.length ? heads[i] : rest!)(item, at(path, i)));
      });
    }
    const item = walkOf(compile((jsonSchema.items ?? {}) as Record<string, unknown>));
    const min = num(jsonSchema.minItems), max = num(jsonSchema.maxItems);
    return check((value, path) => {
      if (!Array.isArray(value)) throw new ValidationError(`expected array, got ${typeName(value)}`, path);
      if (min !== undefined && value.length < min) throw new ValidationError(`fewer than ${min} items`, path);
      if (max !== undefined && value.length > max) throw new ValidationError(`more than ${max} items`, path);
      return value.map((v, i) => item(v, at(path, i)));
    });
  }

  if (type === "object" || jsonSchema.properties) {
    const properties = (jsonSchema.properties ?? {}) as Record<string, Record<string, unknown>>;
    const additional = jsonSchema.additionalProperties;
    if (Object.keys(properties).length === 0 && additional && typeof additional === "object") {
      const value = walkOf(compile(additional as Record<string, unknown>));       // dict[str, T]
      return check((v, path) => {
        if (!isPlain(v)) throw new ValidationError(`expected object, got ${typeName(v)}`, path);
        return Object.fromEntries(Object.entries(v as object).map(([k, x]) => [k, value(x, at(path, k))]));
      });
    }
    const required = Array.isArray(jsonSchema.required) ? new Set(jsonSchema.required as string[]) : null;
    const fields: Record<string, Validator> = {};
    const optional = new Set<string>();
    for (const [key, propSchema] of Object.entries(properties)) {
      fields[key] = compile(propSchema);
      if (required !== null && !required.has(key) && !("default" in propSchema)) optional.add(key);
    }
    const catchall = additional && typeof additional === "object"
      ? walkOf(compile(additional as Record<string, unknown>)) : null;
    return check((value, path) => {
      if (!isPlain(value)) throw new ValidationError(`expected object, got ${typeName(value)}`, path);
      const input = value as Record<string, unknown>;
      const out: Record<string, unknown> = {};
      for (const [key, field] of Object.entries(fields)) {
        if (!(key in input)) {
          if (optional.has(key)) continue;
          const filled = walkOf(field)(undefined, at(path, key));               // a default, or a throw
          if (filled !== undefined) out[key] = filled;
          continue;
        }
        out[key] = walkOf(field)(input[key], at(path, key));
      }
      for (const [key, extra] of Object.entries(input)) {
        if (key in fields) continue;
        if (additional === false) throw new ValidationError(`unexpected key '${key}'`, path);
        out[key] = catchall ? catchall(extra, at(path, key)) : extra;           // passthrough
      }
      return out;
    }, { fields });
  }

  return check((value) => value);                                               // unknown: accept
}

const isPlain = (v: unknown): boolean =>
  typeof v === "object" && v !== null && !Array.isArray(v);

const num = (v: unknown): number | undefined => (typeof v === "number" ? v : undefined);

/** The same validator, returning frozen values: atomdoc's frozen value types. */
export function readonlyOf(inner: Validator): Validator {
  const walk = walkOf(inner);
  return check((value, path) => {
    const parsed = walk(value, path);
    return parsed && typeof parsed === "object" ? Object.freeze(parsed) : parsed;
  }, { fields: inner.fields, nullable: inner.nullable });
}
