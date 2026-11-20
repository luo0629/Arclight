import type { SeriesOutlineContent } from "@/features/story/series-plan";
import type { ProductionPage } from "./production-types";

export type ApprovedSeriesPlan = {
  id: string;
  episode_id: string;
  version_no: number;
  content: SeriesOutlineContent;
  characters: { id: string; name: string }[];
};

export type SeriesRunSummary = {
  id: string;
  project_id: string;
  episode_id: string;
  episode_no: number;
  status: string;
  paused: boolean;
  created_at: string;
};

export type SeriesWorkspace = ProductionPage<SeriesRunSummary> & {
  project_id: string;
  plan: ApprovedSeriesPlan | null;
  ledger_version_id: string | null;
  ledger_head_revision: number;
};
