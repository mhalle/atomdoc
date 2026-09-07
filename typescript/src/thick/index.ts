export { LocalDoc, RefIntegrityError, ListenerError, type ChangeEvent } from "./local-doc.js";
export { type DocNode, createDocNode, getSlotChildren } from "./doc-node.js";
export {
  UndoManager,
  type UndoManagerOptions,
  type UndoHistory,
  type UndoHistoryItem,
} from "./undo-manager.js";
export {
  ThickAtomDocClient,
  type ThickClientOptions,
} from "./thick-client.js";
export { bridgeDocToStore } from "./store-bridge.js";
export {
  createNodeIdFactory,
  numberToBase64,
  incrementBase64,
  randomBase64,
} from "./node-id.js";
export { mergeOperations, type Diff, type OpsAccumulator } from "./local-ops.js";
export { type LifecycleStage, type TransactionFlags } from "./local-transaction.js";
