export { AtomDocClient } from "./client.js";
export { Transaction } from "./transaction.js";
export { NodeStore } from "./store.js";
export { SchemaRegistry } from "./schema.js";
export { applyPatch } from "./patch.js";
export {
  setField,
  deleteNode,
  moveNode,
  createNode,
  undo,
  redo,
} from "./operations.js";
export type {
  StoreNode,
  InsertOp,
  DeleteOp,
  MoveOp,
  OrderedOp,
  WireOperations,
  SchemaMsg,
  SnapshotMsg,
  PatchMsg,
  ErrorMsg,
  ServerMsg,
  OpMsg,
  CreateMsg,
  UndoMsg,
  RedoMsg,
  ClientMsg,
  NodeTypeDef,
  RefDef,
  HandleDef,
  ValueTypeDef,
  AtomDocSchema,
  JsonDoc,
} from "./types.js";
export {
  defineNode,
  defineValue,
  defineHandle,
  buildSchema,
  type NodeDef,
  type ValueDef,
  type FieldDef,
  type FieldType,
} from "./define.js";

// Thick client (offline-capable)
export {
  ThickAtomDocClient,
  LocalDoc,
  RefIntegrityError,
  UndoManager,
  bridgeDocToStore,
  createNodeIdFactory,
  mergeOperations,
  type ThickClientOptions,
  type ChangeEvent,
  type DocNode,
  type Diff,
  type LifecycleStage,
  type TransactionFlags,
  type UndoManagerOptions,
  type UndoHistory,
} from "./thick/index.js";
