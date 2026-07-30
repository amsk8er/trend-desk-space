// frontend/src/stages/registry.tsx
// Maps a pipeline node id -> its stage component. Direct (static) imports so the
// production build is fully tree-shakeable and contains no async chunks.
import type { ComponentType } from "react";
import ImportStage from "./ImportStage";
import OcrStage from "./OcrStage";
import ReviewStage from "./ReviewStage";
import OcrAggregate from "./OcrAggregate";
import PrescreenStage from "./PrescreenStage";
import PositionsStage from "./PositionsStage";
import BFilterStage from "./BFilterStage";
import ExitStage from "./ExitStage";
import ReportStage from "./ReportStage";
import PushStage from "./PushStage";

export type StageComponent = ComponentType<{ batchId: string | null }>;

export const registry: Record<string, StageComponent> = {
  import: ImportStage,
  ocr: OcrStage,
  review: ReviewStage,
  aggregate: OcrAggregate,
  prescreen: PrescreenStage,
  positions: PositionsStage,
  b_filter: BFilterStage,
  exit_check: ExitStage,
  report: ReportStage,
  push: PushStage,
};
