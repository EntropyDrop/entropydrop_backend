from dataclasses import dataclass
from types import MappingProxyType


SKING_DDJ_V104B = "SKING_DDJ_v104b"
SKING_DDJ_V104 = "SKING_DDJ_v104"
SKING_DDJ_V61B = "SKING_DDJ_v61b"
SKING_DDJ_MODEL_PREFIX = "SKING_DDJ_"


@dataclass(frozen=True)
class SkinPipelineSpec:
    prompt_file: str
    template_files: tuple[str, ...]
    provider_model: str
    image_size: str
    aspect_ratio: str
    dense_uv_checkpoint_file: str
    DMR_mappings_dir: str

    def to_task_payload(self) -> dict[str, object]:
        """Return primitives that can safely cross the RQ boundary."""
        return {
            "prompt_file": self.prompt_file,
            "template_files": list(self.template_files),
            "provider_model": self.provider_model,
            "image_size": self.image_size,
            "aspect_ratio": self.aspect_ratio,
            "dense_uv_checkpoint_file": self.dense_uv_checkpoint_file,
            "DMR_mappings_dir": self.DMR_mappings_dir,
        }


MODEL_PIPELINES = MappingProxyType(
    {
        SKING_DDJ_V104B: SkinPipelineSpec(
            prompt_file="real_to_render3.zh-hans.txt",
            template_files=(
                "template51.png",
                "template66.png",
                "template67.png",
                "template68.png",
                "template73.png",
            ),
            provider_model="nano-banana-pro",
            image_size="1K",
            aspect_ratio="1:1",
            dense_uv_checkpoint_file="SKING_DDJ_v104/parser.pt",
            DMR_mappings_dir="mappings_256x512",
        ),
    }
)


def is_sking_ddj_model(model_version: str | None) -> bool:
    return bool(model_version and model_version in MODEL_PIPELINES)


def get_pipeline(model_version: str) -> SkinPipelineSpec:
    try:
        return MODEL_PIPELINES[model_version]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported skin pipeline model: {model_version!r}"
        ) from exc
