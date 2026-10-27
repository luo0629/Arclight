import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { ChoiceSelect } from "@/components/choice-select";
import {
  type VisualAsset,
  type SceneProfile,
  type PropProfile,
  type ReferenceImage,
} from "./api";
import AssetProfileDialog from "./AssetProfileDialog";
import ListPagination from "./ListPagination";
import usePagedList from "./usePagedList";

type Props = {
  asset: VisualAsset;
  path: string;
  writable: boolean;
  onSaved: (row: VisualAsset) => Promise<void>;
};
const sceneEmpty: SceneProfile = {
  layout: "",
  time_of_day: "",
  lighting: "",
  palette: "",
  environment: "",
  reference: null,
};
const propEmpty: PropProfile = {
  appearance: "",
  story_state: "",
  holder: null,
};
const sceneFields = [
  ["layout", "空间布局", 4000],
  ["time_of_day", "时间段", 160],
  ["lighting", "灯光", 2000],
  ["palette", "场景配色", 1000],
  ["environment", "环境描述", 2000],
] as const;
const propFields = [
  ["appearance", "关键道具外观", 2000],
  ["story_state", "剧情状态", 2000],
] as const;

export default function ScenePropPanel(props: Props) {
  const { asset, writable } = props;
  const [editing, setEditing] = useState(false);
  const scene = asset.asset_type === "SCENE";
  const data = scene
    ? (asset.scene_profile ?? sceneEmpty)
    : (asset.prop_profile ?? propEmpty);
  const reference = "reference" in data ? data.reference : data.holder;
  return (
    <section
      className="flex min-w-0 flex-col gap-3"
      aria-label={scene ? "场景设定" : "道具设定"}
    >
      <h3>{scene ? "场景设定" : "道具设定"}</h3>
      <dl className="grid gap-3 break-all text-sm">
        {(scene ? sceneFields : propFields).map(([key, name]) => (
          <div key={key}>
            <dt>{name}</dt>
            <dd className="whitespace-pre-wrap">
              {(data as unknown as Record<string, string>)[key] || "未填写"}
            </dd>
          </div>
        ))}
        <div>
          <dt>{scene ? "精确场景参考版本" : "持有角色精确版本"}</dt>
          <dd>{reference?.asset_version_id ?? "未指定"}</dd>
        </div>
      </dl>
      <p className="text-sm text-muted-foreground">
        {scene
          ? "同一精确场景版本可在多个镜头复用，设定修改不重写已保存镜头。"
          : "持有者绑定本项目已批准角色的精确版本；剧情状态由人工维护。"}
      </p>
      {writable && (
        <Button
          disabled={["ARCHIVED", "LOCKED"].includes(asset.status)}
          onClick={() => setEditing(true)}
        >
          {scene ? "编辑场景设定" : "编辑道具设定"}
        </Button>
      )}
      {editing &&
        (scene ? (
          <SceneEditor {...props} onDismiss={() => setEditing(false)} />
        ) : (
          <PropEditor {...props} onDismiss={() => setEditing(false)} />
        ))}
    </section>
  );
}

function ExactReferenceChoice({
  asset,
  kind,
  value,
  onChange,
}: {
  asset: VisualAsset;
  kind: "SCENE" | "CHARACTER";
  value: { asset_id: string; asset_version_id: string } | null;
  onChange: (
    value: { asset_id: string; asset_version_id: string } | null,
  ) => void;
}) {
  const list = usePagedList<ReferenceImage>(
    "/projects/" + asset.project_id + "/reference-images",
  );
  useEffect(() => {
    void list.reload().catch(() => {});
  }, [list.reload]);
  const available = list.rows.filter(
    (row) =>
      row.asset_type === kind &&
      !row.stale &&
      ["APPROVED", "LOCKED"].includes(row.status) &&
      (kind !== "SCENE" || row.asset_id === asset.id),
  );
  const label = kind === "SCENE" ? "精确场景参考版本" : "持有角色精确版本";
  const options = [
    { value: "NONE", label: "未指定" },
    ...available.map((row) => ({
      value: row.id,
      label: row.asset_name + " · V" + row.version_no + " · " + row.status,
    })),
  ];
  if (value && !options.some((row) => row.value === value.asset_version_id))
    options.push({
      value: value.asset_version_id,
      label: "已绑定版本 · " + value.asset_version_id,
    });
  return (
    <FieldGroup>
      <Field>
        <FieldLabel htmlFor="exact-profile-reference">{label}</FieldLabel>
        <ChoiceSelect
          id="exact-profile-reference"
          aria-label={label}
          value={value?.asset_version_id ?? "NONE"}
          options={options}
          onValueChange={(id) => {
            const row = available.find((item) => item.id === id);
            if (id === "NONE") onChange(null);
            else if (row)
              onChange({ asset_id: row.asset_id, asset_version_id: row.id });
          }}
        />
      </Field>
      <ListPagination label="设定参考版本" count={list.rows.length} {...list} />
    </FieldGroup>
  );
}

function SceneEditor({
  asset,
  onDismiss,
  ...props
}: Props & { onDismiss: () => void }) {
  const [initial] = useState(asset.scene_profile ?? sceneEmpty);
  const [data, setData] = useState(initial);
  return (
    <AssetProfileDialog
      {...props}
      asset={asset}
      title="编辑场景设定"
      endpoint="scene-profile"
      profile={data}
      initial={initial}
      onDismiss={onDismiss}
    >
      <FieldGroup>
        {sceneFields.map(([key, label, max]) => (
          <Field key={key}>
            <FieldLabel htmlFor={key}>{label}</FieldLabel>
            <Textarea
              id={key}
              maxLength={max}
              value={data[key]}
              onChange={(event) =>
                setData({ ...data, [key]: event.target.value })
              }
            />
          </Field>
        ))}
        <ExactReferenceChoice
          asset={asset}
          kind="SCENE"
          value={data.reference}
          onChange={(reference) => setData({ ...data, reference })}
        />
      </FieldGroup>
    </AssetProfileDialog>
  );
}
function PropEditor({
  asset,
  onDismiss,
  ...props
}: Props & { onDismiss: () => void }) {
  const [initial] = useState(asset.prop_profile ?? propEmpty);
  const [data, setData] = useState(initial);
  return (
    <AssetProfileDialog
      {...props}
      asset={asset}
      title="编辑道具设定"
      endpoint="prop-profile"
      profile={data}
      initial={initial}
      onDismiss={onDismiss}
    >
      <FieldGroup>
        {propFields.map(([key, label, max]) => (
          <Field key={key}>
            <FieldLabel htmlFor={key}>{label}</FieldLabel>
            <Textarea
              id={key}
              maxLength={max}
              value={data[key]}
              onChange={(event) =>
                setData({ ...data, [key]: event.target.value })
              }
            />
          </Field>
        ))}
        <ExactReferenceChoice
          asset={asset}
          kind="CHARACTER"
          value={data.holder}
          onChange={(holder) => setData({ ...data, holder })}
        />
      </FieldGroup>
    </AssetProfileDialog>
  );
}
