import { describe, it, expect } from "vitest";
import { defineNode, defineValue, defineHandle, buildSchema } from "../src/define.js";
import { SchemaRegistry } from "../src/schema.js";
import { LocalDoc } from "../src/thick/local-doc.js";
import { getSlotChildren } from "../src/thick/doc-node.js";
import type { AtomDocSchema } from "../src/types.js";

// Define a schema entirely in TypeScript
const Color = defineValue("Color", {
  r: { type: "integer", default: 0 },
  g: { type: "integer", default: 0 },
  b: { type: "integer", default: 0 },
}, { frozen: true });

const Annotation = defineNode("Annotation", {
  label: { type: "string", default: "" },
  color: { type: "object", schema: Color, tier: "atomic", default: { r: 0, g: 0, b: 0 } },
});

const Page = defineNode("Page", {
  title: { type: "string", default: "" },
}, {
  slots: { annotations: "Annotation" },
});

const schema = buildSchema("Page", [Page, Annotation], [Color]);

describe("defineValue", () => {
  it("creates a value definition", () => {
    expect(Color.name).toBe("Color");
    expect(Color.frozen).toBe(true);
    expect(Color.fields.r.type).toBe("integer");
    expect(Color.fields.r.default).toBe(0);
  });
});

describe("defineNode", () => {
  it("creates a node definition with fields", () => {
    expect(Annotation.name).toBe("Annotation");
    expect(Annotation.fields.label.type).toBe("string");
    expect(Annotation.fields.color.tier).toBe("atomic");
    expect(Annotation.slots).toEqual({});
  });

  it("creates a node definition with slots", () => {
    expect(Page.name).toBe("Page");
    expect(Page.slots).toEqual({ annotations: "Annotation" });
  });
});

describe("buildSchema", () => {
  it("produces a valid AtomDocSchema", () => {
    expect(schema.version).toBe(1);
    expect(schema.root_type).toBe("Page");
  });

  it("includes node types", () => {
    expect(schema.node_types.Page).toBeDefined();
    expect(schema.node_types.Annotation).toBeDefined();
  });

  it("includes value types", () => {
    expect(schema.value_types.Color).toBeDefined();
    expect(schema.value_types.Color.frozen).toBe(true);
  });

  it("node type has field tiers", () => {
    expect(schema.node_types.Annotation.field_tiers.label).toBe("mergeable");
    expect(schema.node_types.Annotation.field_tiers.color).toBe("atomic");
  });

  it("node type has slots", () => {
    expect(schema.node_types.Page.slots.annotations.allowed_type).toBe("Annotation");
  });

  it("node type has field defaults", () => {
    expect(schema.node_types.Annotation.field_defaults.label).toBe("");
    expect(schema.node_types.Annotation.field_defaults.color).toEqual({ r: 0, g: 0, b: 0 });
  });

  it("node type has json_schema", () => {
    const props = schema.node_types.Annotation.json_schema.properties as any;
    expect(props.label.type).toBe("string");
    expect(props.color.type).toBe("object");
    expect(props.color.properties.r.type).toBe("integer");
  });

  it("value type has json_schema", () => {
    const props = schema.value_types.Color.json_schema.properties as any;
    expect(props.r.type).toBe("integer");
    expect(props.g.type).toBe("integer");
    expect(props.b.type).toBe("integer");
  });
});

describe("schema compatibility", () => {
  it("works with SchemaRegistry", () => {
    const reg = new SchemaRegistry(schema);
    expect(reg.nodeTypeNames()).toEqual(["Page", "Annotation"]);
    expect(reg.valueTypeNames()).toEqual(["Color"]);
    expect(reg.getFieldTier("Annotation", "color")).toBe("atomic");
    expect(reg.getSlots("Page").annotations.allowed_type).toBe("Annotation");
  });

  it("Zod validation works", () => {
    const reg = new SchemaRegistry(schema);
    const result = reg.validate("Color", { r: 255, g: 0, b: 0 });
    expect(result).toEqual({ r: 255, g: 0, b: 0 });
  });

  it("Zod rejects invalid data", () => {
    const reg = new SchemaRegistry(schema);
    expect(() => reg.validate("Annotation", { label: 123 })).toThrow();
  });

  it("works with LocalDoc", () => {
    const snapshot = [
      "01jqp00000000000000000000",
      "Page",
      { title: "Hello" },
      {
        annotations: [
          ["a1", "Annotation", { label: "First", color: { r: 255, g: 0, b: 0 } }],
        ],
      },
    ] as any;

    const doc = new LocalDoc(schema, snapshot);
    expect(doc.root.state.title).toBe("Hello");

    const children = getSlotChildren(doc.root, "annotations");
    expect(children.length).toBe(1);
    expect(children[0].state.label).toBe("First");
    expect(children[0].state.color).toEqual({ r: 255, g: 0, b: 0 });
  });

  it("LocalDoc can create nodes using TS-defined schema", () => {
    const snapshot = [
      "01jqp00000000000000000000",
      "Page",
      { title: "Test" },
      { annotations: [] },
    ] as any;

    const doc = new LocalDoc(schema, snapshot);
    const ann = doc.createNode("Annotation", { label: "New", color: { r: 0, g: 255, b: 0 } });
    doc.insertIntoSlot(doc.root, "annotations", "append", [ann]);

    const children = getSlotChildren(doc.root, "annotations");
    expect(children.length).toBe(1);
    expect(children[0].state.label).toBe("New");
    expect(children[0].state.color).toEqual({ r: 0, g: 255, b: 0 });
  });

  it("produces same format as Python atomdoc_schema", () => {
    // The schema should be JSON-serializable and match the wire format
    const json = JSON.parse(JSON.stringify(schema));
    expect(json.version).toBe(1);
    expect(json.root_type).toBe("Page");
    expect(json.node_types.Page.slots.annotations.allowed_type).toBe("Annotation");
    expect(json.value_types.Color.frozen).toBe(true);
  });
});

describe("self-referential nodes", () => {
  it("supports self-referential slot types", () => {
    const Item = defineNode("Item", {
      label: { type: "string", default: "" },
    }, {
      slots: { children: "Item" },
    });

    const App = defineNode("App", {
      title: { type: "string", default: "" },
    }, {
      slots: { items: "Item" },
    });

    const treeSchema = buildSchema("App", [App, Item]);
    expect(treeSchema.node_types.Item.slots.children.allowed_type).toBe("Item");

    // Can create a LocalDoc from this schema
    const snapshot = [
      "01jqp00000000000000000000",
      "App",
      { title: "Tree" },
      {
        items: [
          ["i1", "Item", { label: "Root Item" }, {
            children: [["i2", "Item", { label: "Child" }, { children: [] }]],
          }],
        ],
      },
    ] as any;

    const doc = new LocalDoc(treeSchema, snapshot);
    const items = getSlotChildren(doc.root, "items");
    expect(items.length).toBe(1);
    const nested = getSlotChildren(items[0], "children");
    expect(nested.length).toBe(1);
    expect(nested[0].state.label).toBe("Child");
  });
});

describe("ref fields", () => {
  const Transform = defineNode("Transform", {
    name: { type: "string", default: "" },
    parent: { type: "ref", target: "Transform", default: null },
  });
  const Volume = defineNode("Volume", {
    transform: { type: "ref", target: "Transform", default: null },
    sources: { type: "ref", target: "Volume", many: true, default: [] },
    tag: { type: "ref", default: null },
  });
  const Scene = defineNode("Scene", {}, {
    slots: { transforms: "Transform", volumes: "Volume" },
  });
  const refSchema = buildSchema("Scene", [Scene, Transform, Volume]);

  it("exports tier 'ref' and a refs block", () => {
    const vol = refSchema.node_types.Volume;
    expect(vol.field_tiers.transform).toBe("ref");
    expect(vol.field_tiers.sources).toBe("ref");
    expect(vol.refs).toEqual({
      transform: { target_type: "Transform", many: false, policy: "restrict" },
      sources: { target_type: "Volume", many: true, policy: "restrict" },
      tag: { target_type: null, many: false, policy: "restrict" },
    });
  });

  it("exports references as node ids in the JSON schema", () => {
    const props = refSchema.node_types.Volume.json_schema.properties as Record<string, unknown>;
    expect(props.transform).toEqual({ anyOf: [{ type: "string" }, { type: "null" }], default: null });
    expect(props.sources).toEqual({ type: "array", items: { type: "string" }, default: [] });
  });

  it("registry exposes ref definitions", () => {
    const registry = new SchemaRegistry(refSchema);
    expect(registry.getRef("Volume", "transform")?.target_type).toBe("Transform");
    expect(registry.getRef("Volume", "nope")).toBeUndefined();
    expect(Object.keys(registry.getRefs("Transform"))).toEqual(["parent"]);
    expect(registry.getRefs("Scene")).toEqual({});
    expect(registry.getFieldTier("Volume", "sources")).toBe("ref");
  });
});

describe("handles and unions", () => {
  const VoxelData = defineHandle("VoxelData", "strong");
  const Term = defineHandle("Term");
  const Volume = defineNode("Volume", {
    data: { type: "object", schema: VoxelData, tier: "atomic", default: null },
    term: { type: "object", schema: Term, tier: "atomic", default: null },
  });
  const built = buildSchema("Volume", [Volume], [VoxelData, Term]);

  it("exports handle strength per field and per value type", () => {
    expect(built.node_types.Volume.handles).toEqual({
      data: { value_type: "VoxelData", strength: "strong" },
      term: { value_type: "Term", strength: "weak" },
    });
    expect(built.value_types.VoxelData.handle).toEqual({ strength: "strong" });
    expect(built.value_types.VoxelData.frozen).toBe(true);
    expect(Object.keys(built.value_types.VoxelData.json_schema.properties as object)).toEqual([
      "uri", "media_type", "digest",
    ]);
    expect(new SchemaRegistry(built).getHandles("Volume").data.strength).toBe("strong");
  });

  it("LocalDoc lists handles by strength", () => {
    const doc = new LocalDoc(built, [
      "01jqp00000000000000000000",
      "Volume",
      { data: { uri: "s3://a", media_type: "", digest: "" }, term: null },
    ]);
    expect(doc.handles("strong").map((h) => [h.field, h.handle.uri])).toEqual([["data", "s3://a"]]);
    expect(doc.handles("weak")).toEqual([]);
    expect(doc.handles().length).toBe(1);
  });

  it("validates a tagged union of value types from an inlined oneOf", () => {
    const unionSchema: AtomDocSchema = {
      version: 1,
      root_type: "Override",
      node_types: {
        Override: {
          json_schema: {
            type: "object",
            properties: {
              value: {
                oneOf: [
                  { type: "object", properties: { kind: { type: "string" }, v: { type: "number" } } },
                  { type: "object", properties: { kind: { type: "string" }, v: { type: "array", items: { type: "number" } } } },
                ],
                default: { kind: "scalar", v: 0 },
              },
              maybe: { anyOf: [{ type: "string" }, { type: "null" }] },
            },
          },
          field_tiers: { value: "atomic", maybe: "mergeable" },
          slots: {},
          field_defaults: { value: { kind: "scalar", v: 0 } },
        },
      },
      value_types: {},
    };
    const registry = new SchemaRegistry(unionSchema);
    expect(registry.validate("Override", { value: { kind: "vec", v: [1, 2, 3] }, maybe: null })).toEqual({
      value: { kind: "vec", v: [1, 2, 3] },
      maybe: null,
    });
    expect(() => registry.validate("Override", { value: { kind: "vec", v: "x" }, maybe: 1 })).toThrow();
  });
});
