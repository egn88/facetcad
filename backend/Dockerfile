# Multi-stage so the (large) build toolchain never reaches the final image.
FROM python:3.12-slim AS builder

# OpenCascade is the production kernel, and the MCP extra makes the running
# server usable by an agent over HTTP without anything installed client-side.
# Set to "." to build a much smaller image that falls back to the analytic
# kernel (axis-aligned prisms only) and serves no MCP endpoint.
ARG INSTALL_TARGET=".[occt,mcp]"

WORKDIR /build
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --upgrade pip

# Dependencies are installed against a stub package so this layer is keyed only
# on pyproject.toml. Without it, editing one line of source re-downloads all of
# OpenCascade — several minutes for a change that affects none of it.
COPY pyproject.toml ./
RUN mkdir -p src/facet && touch src/facet/__init__.py \
 && /opt/venv/bin/pip install --no-cache-dir "${INSTALL_TARGET}"

# Now the real source, installed over the stub without touching dependencies.
COPY src ./src
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps --force-reinstall .

# cadquery-ocp ships the whole of CadQuery's runtime, including VTK for its
# viewer and matplotlib for its plots. This project tessellates itself and
# renders in the browser, so none of it is ever imported — verified by loading
# every OCP module used here and checking nothing pulls them in. Together they
# are roughly 500MB of a 1.2GB image, which is most of what a person waits for
# on a first pull.
#
# pip and setuptools go too: the runtime stage installs nothing.
RUN cd /opt/venv/lib/python3.12/site-packages \
 && rm -rf matplotlib matplotlib.libs mpl_toolkits pylab.py \
           fontTools PIL pillow.libs kiwisolver* cycler* contourpy* \
           pip pip-* setuptools setuptools-* pkg_resources wheel wheel-* \
 && find /opt/venv -name "__pycache__" -type d -prune -exec rm -rf {} + \
 && find /opt/venv -name "*.pyc" -delete

# VTK cannot simply be deleted: OCP links against OCCT's libTKIVtk, which needs
# VTK's shared objects at load time even though nothing here ever imports one.
# Delete them and the module fails to load — and the application then falls back
# to the analytic kernel and quietly builds a *different*, much simpler part.
#
# So keep exactly the libraries in OCP's resolved link chain and drop the rest,
# which is mostly VTK-m's accelerator filters. Derived from ldd rather than
# guessed, and the build fails below if the result cannot do real geometry.
RUN cd /opt/venv/lib/python3.12/site-packages \
 && ldd OCP/OCP.cpython-312-*.so | grep -o "vtkmodules/[^ ]*\.so[^ ]*" \
      | xargs -n1 basename | sort -u > /tmp/keep.txt \
 && find vtkmodules -maxdepth 1 -name "*.so*" -printf "%f\n" | sort -u > /tmp/have.txt \
 && comm -13 /tmp/keep.txt /tmp/have.txt | sed "s|^|vtkmodules/|" | xargs -r rm -f \
 && rm -f /tmp/keep.txt /tmp/have.txt

FROM python:3.12-slim AS runtime

# OpenCascade needs these at runtime; harmless when the analytic kernel is used.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglu1-mesa libxrender1 libxext6 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# A pruned image that imports OCP but cannot cut a fillet is worse than a large
# one, because the fallback to the analytic kernel is silent: the model still
# builds, as a different and much simpler part. Checked here rather than in the
# builder, which has none of the GL libraries installed above and so cannot load
# OCP at all.
RUN /opt/venv/bin/python -c "\
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox; \
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet; \
from OCP.STEPControl import STEPControl_Writer; \
from OCP.TopExp import TopExp_Explorer; \
from OCP.TopAbs import TopAbs_EDGE; \
from OCP.TopoDS import TopoDS; \
box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(); \
mk = BRepFilletAPI_MakeFillet(box); \
e = TopExp_Explorer(box, TopAbs_EDGE); \
mk.Add(1.0, TopoDS.Edge_s(e.Current())); \
mk.Build(); \
assert mk.IsDone(), 'the pruned image cannot cut a fillet'; \
STEPControl_Writer(); \
print('OCCT verified: fillet and STEP both work')"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FACET_DATA=/data/projects

# Never run as root.
RUN useradd --create-home --uid 10001 facet \
 && mkdir -p /data/projects \
 && chown -R facet /data
USER facet
WORKDIR /app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

CMD ["uvicorn", "facet.main:app", "--host", "0.0.0.0", "--port", "8000"]
