import { notFound } from "next/navigation";
import { isResourceId } from "@/lib/routes";
import EpisodePage from "@/features/episodes/components/episode-page";

export default async function Page({
  params,
}: {
  params: Promise<{ projectId: string; episodeId: string }>;
}) {
  const { episodeId } = await params;
  if (!isResourceId(episodeId)) notFound();
  return <EpisodePage key={episodeId} />;
}
