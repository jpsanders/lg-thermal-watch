FROM python:3.15-slim

ARG USER=appuser
ARG GROUP=appuser

WORKDIR /app

# Add non-root user
RUN useradd -rms /bin/bash $USER && usermod -aG users $GROUP
RUN chown -R $USER:$GROUP /app

ENV PATH="/home/${USER}/.local/bin:${PATH}"

USER $USER

COPY --chown=$USER:$GROUP src src
COPY --chown=$USER:$GROUP pyproject.toml .
RUN --mount=source=.git,target=.git,type=bind \
    python3 -m pip install .
# Every API call needs the token (LG_THERMAL_WATCH_TOKEN, or the file `lg-thermal-watch token` prints), so the
# container can listen on all of its interfaces.
ENV LG_THERMAL_WATCH_HOST=0.0.0.0
EXPOSE 8000
CMD ["lg-thermal-watch"]
