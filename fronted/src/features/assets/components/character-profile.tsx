"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { ChoiceSelect } from "@/components/choice-select";
import type {
  VisualAsset,
  CharacterProfile,
  ReferenceImage,
} from "@/types/api";
import AssetProfileDialog from "@/features/assets/components/asset-profile-dialog";
import usePagedList from "@/hooks/use-paged-list";
import ListPagination from "@/components/shared/list-pagination";

const empty: CharacterProfile = {
  fixed_features: [],
  variable_features: [],
  expressions: [],
  outfits: [],
  default_outfit_id: null,
  images: [],
  standard_image_id: null,
};
export default function CharacterPanel({
  asset,
  path,
  writable,
  onSaved,
}: {
  asset: VisualAsset;
  path: string;
  writable: boolean;
  onSaved: (row: VisualAsset) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const data = asset.character_profile ?? empty;
  return (
    <section aria-label="角色设定" className="flex min-w-0 flex-col gap-3">
      <h3>角色设定</h3>
      <dl className="grid gap-3 break-all text-sm">
        <div>
          <dt>固定特征</dt>
          <dd>{data.fixed_features.join("、") || "未填写"}</dd>
        </div>
        <div>
          <dt>允许变化特征</dt>
          <dd>{data.variable_features.join("、") || "未填写"}</dd>
        </div>
        <div>
          <dt>表情</dt>
          <dd>{data.expressions.join("、") || "未填写"}</dd>
        </div>
        <div>
          <dt>服装</dt>
          <dd>
            {data.outfits.length
              ? data.outfits.map((item) => (
                  <p key={item.id}>
                    {item.name}
                    {item.id === data.default_outfit_id
                      ? "（默认）"
                      : "（可选）"}{" "}
                    · {item.description}
                  </p>
                ))
              : "未填写"}
          </dd>
        </div>
        <div>
          <dt>精确参考图</dt>
          <dd>
            {data.images.length
              ? data.images.map((item) => (
                  <p key={item.id}>
                    {item.label} ·{" "}
                    {item.kind === "INITIAL" ? "初始设定" : item.angle}{" "}
                    {item.id === data.standard_image_id && "· 标准参考"} ·{" "}
                    {item.source.asset_version_id}
                  </p>
                ))
              : "尚未登记"}
          </dd>
        </div>
      </dl>
      <p className="text-sm text-muted-foreground">
        参考图绑定已有精确版本；标准参考须人工指定已批准版本。图片生成与文件预览尚未接入。
      </p>
      {writable && (
        <Button
          disabled={["ARCHIVED", "LOCKED"].includes(asset.status)}
          onClick={() => setEditing(true)}
        >
          编辑角色设定
        </Button>
      )}
      {editing && (
        <CharacterEditor
          asset={asset}
          path={path}
          writable={writable}
          onSaved={onSaved}
          onDismiss={() => setEditing(false)}
        />
      )}
    </section>
  );
}

function FeatureLines({
  name,
  values,
  onChange,
}: {
  name: string;
  values: string[];
  onChange: (values: string[]) => void;
}) {
  const [text, setText] = useState(values.join(String.fromCharCode(10)));
  return (
    <Field>
      <FieldLabel htmlFor={"features-" + name}>{name}（每行一项）</FieldLabel>
      <Textarea
        id={"features-" + name}
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          onChange(
            event.target.value
              .split(String.fromCharCode(10))
              .map((value) => value.trim())
              .filter(Boolean),
          );
        }}
      />
    </Field>
  );
}

function CharacterEditor({
  asset,
  path,
  writable,
  onSaved,
  onDismiss,
}: {
  asset: VisualAsset;
  path: string;
  writable: boolean;
  onSaved: (row: VisualAsset) => Promise<void>;
  onDismiss: () => void;
}) {
  const [initial] = useState(asset.character_profile ?? empty);
  const [data, setData] = useState(initial);
  const references = usePagedList<ReferenceImage>(
    "/projects/" + asset.project_id + "/reference-images",
  );
  useEffect(() => {
    void references.reload().catch(() => {});
  }, [references.reload]);
  const [sourceId, setSourceId] = useState("");
  const [imageKind, setImageKind] = useState("ANGLE");
  const [label, setLabel] = useState("");
  const [angle, setAngle] = useState("");
  const available = references.rows.filter(
    (row) =>
      !row.stale &&
      ["DRAFT", "READY_FOR_REVIEW", "APPROVED", "LOCKED"].includes(
        row.status,
      ) &&
      (["IMAGE", "SHOT_IMAGE"].includes(row.asset_type) ||
        row.asset_id === asset.id),
  );
  const source = available.find((row) => row.id === sourceId);
  function addImage() {
    if (!source || !label.trim() || (imageKind === "ANGLE" && !angle.trim()))
      return;
    setData({
      ...data,
      images: [
        ...data.images,
        {
          id: crypto.randomUUID(),
          label: label.trim(),
          kind: imageKind as "ANGLE" | "INITIAL",
          angle: angle.trim(),
          source: { asset_id: source.asset_id, asset_version_id: source.id },
        },
      ],
    });
    setLabel("");
    setAngle("");
    setSourceId("");
  }
  return (
    <AssetProfileDialog
      asset={asset}
      path={path}
      endpoint="character-profile"
      title="编辑角色设定"
      profile={data}
      initial={initial}
      pendingDirty={!!label || !!angle || !!sourceId}
      writable={writable}
      onSaved={onSaved}
      onDismiss={onDismiss}
    >
      <FieldGroup>
        {(
          [
            ["fixed_features", "固定特征"],
            ["variable_features", "允许变化特征"],
            ["expressions", "表情"],
          ] as const
        ).map(([key, name]) => (
          <FeatureLines
            key={key}
            name={name}
            values={data[key]}
            onChange={(values) => setData({ ...data, [key]: values })}
          />
        ))}
        <p>固定与可变特征不能重叠。服装列表中未指定默认的均为可选服装。</p>
        {data.outfits.map((item, index) => (
          <FieldGroup key={item.id}>
            <Field>
              <FieldLabel htmlFor={"outfit-" + item.id}>
                服装{index + 1}名称
              </FieldLabel>
              <Input
                id={"outfit-" + item.id}
                required
                maxLength={80}
                value={item.name}
                onChange={(event) =>
                  setData({
                    ...data,
                    outfits: data.outfits.map((row) =>
                      row.id === item.id
                        ? { ...row, name: event.target.value }
                        : row,
                    ),
                  })
                }
              />
            </Field>
            <Field>
              <FieldLabel htmlFor={"outfit-description-" + item.id}>
                服装{index + 1}说明
              </FieldLabel>
              <Textarea
                id={"outfit-description-" + item.id}
                maxLength={1000}
                value={item.description}
                onChange={(event) =>
                  setData({
                    ...data,
                    outfits: data.outfits.map((row) =>
                      row.id === item.id
                        ? { ...row, description: event.target.value }
                        : row,
                    ),
                  })
                }
              />
            </Field>
            <Button
              type="button"
              variant="outline"
              onClick={() =>
                setData({
                  ...data,
                  outfits: data.outfits.filter((row) => row.id !== item.id),
                  default_outfit_id:
                    data.default_outfit_id === item.id
                      ? null
                      : data.default_outfit_id,
                })
              }
            >
              移除服装{index + 1}
            </Button>
          </FieldGroup>
        ))}
        <Button
          type="button"
          variant="outline"
          disabled={data.outfits.length >= 20}
          onClick={() =>
            setData({
              ...data,
              outfits: [
                ...data.outfits,
                { id: crypto.randomUUID(), name: "", description: "" },
              ],
            })
          }
        >
          添加可选服装
        </Button>
        <Field>
          <FieldLabel htmlFor="default-outfit">默认服装</FieldLabel>
          <ChoiceSelect
            id="default-outfit"
            aria-label="默认服装"
            value={data.default_outfit_id ?? "NONE"}
            options={[
              { value: "NONE", label: "未指定" },
              ...data.outfits.map((item, index) => ({
                value: item.id,
                label: item.name || "服装" + (index + 1),
              })),
            ]}
            onValueChange={(value) =>
              setData({
                ...data,
                default_outfit_id: value === "NONE" ? null : value,
              })
            }
          />
        </Field>
        <h3>参考图记录</h3>
        {data.images.map((item) => (
          <div key={item.id} className="flex min-w-0 flex-col gap-2 break-all">
            <p>
              {item.label} ·{" "}
              {item.kind === "INITIAL" ? "初始设定图" : item.angle} ·{" "}
              {item.source.asset_version_id}
            </p>
            <Button
              type="button"
              variant="outline"
              onClick={() =>
                setData({
                  ...data,
                  images: data.images.filter((row) => row.id !== item.id),
                  standard_image_id:
                    data.standard_image_id === item.id
                      ? null
                      : data.standard_image_id,
                })
              }
            >
              移除参考图 {item.label}
            </Button>
          </div>
        ))}
        <Field>
          <FieldLabel htmlFor="reference-source">已有精确图片版本</FieldLabel>
          <ChoiceSelect
            id="reference-source"
            aria-label="已有精确图片版本"
            value={sourceId || "NONE"}
            options={[
              { value: "NONE", label: "选择已载入版本" },
              ...available.map((row) => ({
                value: row.id,
                label:
                  row.asset_name + " · V" + row.version_no + " · " + row.status,
              })),
            ]}
            onValueChange={(value) =>
              setSourceId(value === "NONE" ? "" : value)
            }
          />
        </Field>
        <ListPagination
          label="参考图版本"
          count={references.rows.length}
          {...references}
        />
        <Field>
          <FieldLabel htmlFor="image-kind">参考图用途</FieldLabel>
          <ChoiceSelect
            id="image-kind"
            aria-label="参考图用途"
            value={imageKind}
            options={[
              { value: "INITIAL", label: "初始设定图" },
              { value: "ANGLE", label: "多角度参考图" },
            ]}
            onValueChange={setImageKind}
          />
        </Field>
        <Field>
          <FieldLabel htmlFor="image-label">参考图名称</FieldLabel>
          <Input
            id="image-label"
            maxLength={80}
            value={label}
            onChange={(event) => setLabel(event.target.value)}
          />
        </Field>
        <Field>
          <FieldLabel htmlFor="image-angle">参考角度</FieldLabel>
          <Input
            id="image-angle"
            maxLength={80}
            value={angle}
            onChange={(event) => setAngle(event.target.value)}
          />
        </Field>
        <Button
          type="button"
          variant="outline"
          disabled={
            !source ||
            !label.trim() ||
            data.images.length >= 30 ||
            (imageKind === "ANGLE" && !angle.trim())
          }
          onClick={addImage}
        >
          添加精确参考图
        </Button>
        <Field>
          <FieldLabel htmlFor="standard-image">标准参考图</FieldLabel>
          <ChoiceSelect
            id="standard-image"
            aria-label="标准参考图"
            value={data.standard_image_id ?? "NONE"}
            options={[
              { value: "NONE", label: "未指定" },
              ...data.images.map((item) => ({
                value: item.id,
                label: item.label,
              })),
            ]}
            onValueChange={(value) =>
              setData({
                ...data,
                standard_image_id: value === "NONE" ? null : value,
              })
            }
          />
        </Field>
      </FieldGroup>
    </AssetProfileDialog>
  );
}
