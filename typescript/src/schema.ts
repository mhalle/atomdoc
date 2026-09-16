/**
 * Schema registry — hydrates the atomdoc schema into validators.
 */

import { compile, readonlyOf, type Validator } from "./validate.js";
import type { AtomDocSchema, HandleDef, NodeTypeDef, RefDef, ValueTypeDef } from "./types.js";

export class SchemaRegistry {
  private nodeTypes: Map<string, NodeTypeDef>;
  private valueTypes: Map<string, ValueTypeDef>;
  private validators = new Map<string, Validator>();

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
  ): Record<string, { allowed_type: string | null; allowed_types?: string[] }> {
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

  /** Get or build the validator for a node or value type. */
  getValidator(typeName: string): Validator | undefined {
    const cached = this.validators.get(typeName);
    if (cached) return cached;

    const nodeDef = this.nodeTypes.get(typeName);
    if (nodeDef) {
      const validator = compile(nodeDef.json_schema);
      this.validators.set(typeName, validator);
      return validator;
    }

    const valueDef = this.valueTypes.get(typeName);
    if (valueDef) {
      const validator = compile(valueDef.json_schema);
      const frozen = valueDef.frozen ? readonlyOf(validator) : validator;
      this.validators.set(typeName, frozen);
      return frozen;
    }

    return undefined;
  }

  /** @deprecated the schema is JSON Schema now, not Zod: use `getValidator`. */
  getZodSchema(typeName: string): Validator | undefined {
    return this.getValidator(typeName);
  }

  /** Validate data against a named type's schema. */
  validate(typeName: string, data: unknown): unknown {
    const schema = this.getValidator(typeName);
    if (!schema) {
      throw new Error(`Unknown type: ${typeName}`);
    }
    return schema.parse(data);
  }

  /**
   * Validate one field's value against the type's schema and return it
   * as parsed (nested defaults filled). Throws a `ValidationError` for a
   * value the server would reject, and an `Error` for an unknown type or
   * field.
   */
  validateField(typeName: string, field: string, value: unknown): unknown {
    const schema = this.getValidator(typeName);
    if (!schema) {
      throw new Error(`Unknown type: ${typeName}`);
    }
    const fieldSchema = schema.fields?.[field];
    if (!fieldSchema) {
      // A field the type declares but its JSON Schema does not describe
      // (a schema exported without properties) has nothing to check.
      const tiers = this.nodeTypes.get(typeName)?.field_tiers ?? {};
      if (field in tiers) return value;
      throw new Error(`Unknown field '${field}' on ${typeName}`);
    }
    return fieldSchema.parse(value);
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
