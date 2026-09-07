/**
 * A Slicer-like scene for the performance tests: transforms with a 4x4
 * matrix and a parent reference, volumes with a transform reference and a
 * strong handle, markups with a point list, nestable folders.
 */

import type { AtomDocSchema, JsonDoc } from "../../src/types.js";

export interface Scenario {
  desc: string;
  /** Multiplier applied to the requested size for slow scenarios. */
  scale?: number;
  /** Returns seconds for the timed part only. */
  run: (n: number) => number;
}

const IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];

export const schema: AtomDocSchema = {
  version: 1,
  root_type: "Scene",
  node_types: {
    Scene: {
      json_schema: { type: "object", properties: { title: { type: "string" } } },
      field_tiers: { title: "mergeable" },
      slots: {
        transforms: { allowed_type: "Transform" },
        volumes: { allowed_type: "Volume" },
        markups: { allowed_type: "Markup" },
        folders: { allowed_type: "Folder" },
      },
      field_defaults: { title: "" },
    },
    Transform: {
      json_schema: {
        type: "object",
        properties: {
          name: { type: "string" },
          matrix: { type: "array", items: { type: "number" }, minItems: 16, maxItems: 16 },
          parent: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
        },
      },
      field_tiers: { name: "mergeable", matrix: "mergeable", parent: "ref" },
      slots: {},
      field_defaults: { name: "", matrix: IDENTITY, parent: null },
      refs: { parent: { target_type: "Transform", many: false, policy: "restrict" } },
    },
    Volume: {
      json_schema: {
        type: "object",
        properties: {
          name: { type: "string" },
          window: { type: "number" },
          level: { type: "number" },
          transform: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
          voxels: {
            anyOf: [
              { type: "object", properties: { uri: { type: "string" }, media_type: { type: "string" }, digest: { type: "string" } }, required: ["uri"] },
              { type: "null" },
            ],
            default: null,
          },
        },
      },
      field_tiers: { name: "mergeable", window: "mergeable", level: "mergeable", transform: "ref", voxels: "atomic" },
      slots: {},
      field_defaults: { name: "", window: 100, level: 50, transform: null, voxels: null },
      refs: { transform: { target_type: "Transform", many: false, policy: "restrict" } },
      handles: { voxels: { value_type: "Voxels", strength: "strong" } },
    },
    Markup: {
      json_schema: { type: "object", properties: { label: { type: "string" }, points: { type: "array" } } },
      field_tiers: { label: "mergeable", points: "mergeable", transform: "ref" },
      slots: {},
      field_defaults: { label: "", points: [], transform: null },
      refs: { transform: { target_type: "Transform", many: false, policy: "restrict" } },
    },
    Folder: {
      json_schema: { type: "object", properties: { name: { type: "string" } } },
      field_tiers: { name: "mergeable" },
      slots: { items: { allowed_type: "Folder" } },
      field_defaults: { name: "" },
    },
  },
  value_types: {
    Voxels: {
      json_schema: { type: "object", properties: { uri: { type: "string" } }, required: ["uri"] },
      frozen: true,
      handle: { strength: "strong" },
    },
  },
};

/** A scene with a hub transform, n transforms pointing at it, n volumes. */
export function makeSnapshot(n: number): JsonDoc {
  const transforms: JsonDoc[] = [["hub", "Transform", { name: "hub" }]];
  const volumes: JsonDoc[] = [];
  for (let i = 0; i < n; i++) {
    transforms.push([`t${i}`, "Transform", { name: `t${i}`, parent: "hub" }]);
    volumes.push([`v${i}`, "Volume", { name: `v${i}`, transform: `t${i}`, voxels: { uri: `file://v${i}.nrrd` } }]);
  }
  return ["01jqp00000000000000000000", "Scene", {}, { transforms, volumes, markups: [], folders: [] }];
}
