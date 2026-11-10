"""Version the controlled renderer contract and record the libraries actually loaded."""

from arclight.domain.render_manifests import RenderSoftware

RENDERER_VERSION = "arclight-pyav/1"


def renderer_software() -> RenderSoftware:
    import av

    return RenderSoftware(
        renderer_version=RENDERER_VERSION,
        pyav_version=av.__version__,
        ffmpeg_libraries={
            name: ".".join(str(part) for part in version)
            for name, version in sorted(av.library_versions.items())
        },
    )
