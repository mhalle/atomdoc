import { describe, it, expect } from "vitest";
import { SchemaRegistry } from "../src/schema.js";
import type { AtomDocSchema } from "../src/types.js";

const testSchema: AtomDocSchema = {
  version: 1,
  root_type: "Page",
  node_types: {
    Page: {
      json_schema: {
        type: "object",
        properties: {
          title: { type: "string", default: "" },
        },
      },
      field_tiers: { title: "mergeable" },
      slots: { annotations: { allowed_type: "Annotation" } },
      field_defaults: { title: "" },
    },
    Annotation: {
      json_schema: {
        type: "object",
        properties: {
          label: { type: "string", default: "" },
          color: {
            type: "object",
            properties: {
              r: { type: "integer", default: 0 },
              g: { type: "integer", default: 0 },
              b: { type: "integer", default: 0 },
            },
          },
        },
      },
      field_tiers: { label: "mergeable", color: "atomic" },
      slots: {},
      field_defaults: { label: "", color: { r: 0, g: 0, b: 0 } },
    },
  },
  value_types: {
    Color: {
      json_schema: {
        type: "object",
        properties: {
          r: { type: "integer", default: 0 },
          g: { type: "integer", default: 0 },
          b: { type: "integer", default: 0 },
        },
      },
      frozen: true,
    },
  },
};

describe("SchemaRegistry", () => {
  it("lists node types", () => {
    const reg = new SchemaRegistry(testSchema);
    expect(reg.nodeTypeNames()).toEqual(["Page", "Annotation"]);
  });

  it("lists value types", () => {
    const reg = new SchemaRegistry(testSchema);
    expect(reg.valueTypeNames()).toEqual(["Color"]);
  });

  it("gets node type definition", () => {
    const reg = new SchemaRegistry(testSchema);
    const page = reg.getNodeType("Page");
    expect(page).toBeDefined();
    expect(page!.field_tiers.title).toBe("mergeable");
  });

  it("gets value type definition", () => {
    const reg = new SchemaRegistry(testSchema);
    const color = reg.getValueType("Color");
    expect(color).toBeDefined();
    expect(color!.frozen).toBe(true);
  });

  it("gets field tier", () => {
    const reg = new SchemaRegistry(testSchema);
    expect(reg.getFieldTier("Annotation", "label")).toBe("mergeable");
    expect(reg.getFieldTier("Annotation", "color")).toBe("atomic");
  });

  it("gets slots", () => {
    const reg = new SchemaRegistry(testSchema);
    const slots = reg.getSlots("Page");
    expect(slots.annotations.allowed_type).toBe("Annotation");
  });

  it("gets defaults", () => {
    const reg = new SchemaRegistry(testSchema);
    expect(reg.getDefaults("Annotation")).toEqual({
      label: "",
      color: { r: 0, g: 0, b: 0 },
    });
  });

  it("builds Zod schema for node type", () => {
    const reg = new SchemaRegistry(testSchema);
    const schema = reg.getZodSchema("Page");
    expect(schema).toBeDefined();
    const result = schema!.parse({ title: "Hello" });
    expect(result).toEqual({ title: "Hello" });
  });

  it("Zod schema rejects invalid data", () => {
    const reg = new SchemaRegistry(testSchema);
    const schema = reg.getZodSchema("Annotation");
    expect(() => schema!.parse({ label: 123 })).toThrow();
  });

  it("builds Zod schema for value type", () => {
    const reg = new SchemaRegistry(testSchema);
    const schema = reg.getZodSchema("Color");
    expect(schema).toBeDefined();
    const result = schema!.parse({ r: 255, g: 0, b: 0 });
    expect(result).toEqual({ r: 255, g: 0, b: 0 });
  });

  it("validates against named type", () => {
    const reg = new SchemaRegistry(testSchema);
    const result = reg.validate("Color", { r: 100, g: 200, b: 50 });
    expect(result).toEqual({ r: 100, g: 200, b: 50 });
  });

  it("throws on unknown type", () => {
    const reg = new SchemaRegistry(testSchema);
    expect(() => reg.validate("Bogus", {})).toThrow("Unknown type");
  });

  it("caches Zod schemas", () => {
    const reg = new SchemaRegistry(testSchema);
    const s1 = reg.getZodSchema("Page");
    const s2 = reg.getZodSchema("Page");
    expect(s1).toBe(s2);
  });

  it("returns undefined for unknown type", () => {
    const reg = new SchemaRegistry(testSchema);
    expect(reg.getZodSchema("Nope")).toBeUndefined();
    expect(reg.getNodeType("Nope")).toBeUndefined();
  });
});
