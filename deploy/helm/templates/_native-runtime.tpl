{{/* Shared protected inputs for every process that verifies native custody. */}}
{{- define "troshka.nativeRuntime.initContainers" -}}
{{- $cliImage := .worker.cliImage | default "" }}
{{- if and $cliImage (or (contains "@" $cliImage) (not (regexMatch ":[^/:]+$" $cliImage)) (hasSuffix ":latest" $cliImage)) }}
{{- fail "Collection CLI image requires an explicit version tag (not latest or a digest)" }}
{{- end }}
{{- if $cliImage }}
- name: prepare-collection-cli
  image: {{ $cliImage | quote }}
  command: ["/bin/sh", "-c", "rm -f /collection-bin/.oc && cp /usr/bin/oc /collection-bin/.oc && chmod 0555 /collection-bin/.oc && mv -f /collection-bin/.oc /collection-bin/oc"]
  volumeMounts:
    - name: collection-bin
      mountPath: /collection-bin
{{- end }}
- name: prepare-collection-config
  image: {{ include "troshka.backendImage" .root }}
  command:
    - python3
    - -c
    - |
      import os
      import tempfile
      from pathlib import Path
      for source, target in [("/config-source", "/protected/config"), ("/transport-source", "/protected/transport")]:
          destination = Path(target)
          destination.mkdir(mode=0o700, parents=True, exist_ok=True)
          for abandoned in destination.glob(".copy-*"):
              abandoned.unlink()
          for entry in Path(source).iterdir():
              if entry.name.startswith(".."):
                  continue
              data = entry.read_bytes()
              path = destination / entry.name
              fd, temporary = tempfile.mkstemp(dir=destination, prefix=".copy-")
              try:
                  os.fchmod(fd, 0o400)
                  with os.fdopen(fd, "wb") as output:
                      output.write(data)
                  os.replace(temporary, path)
              finally:
                  if os.path.exists(temporary):
                      os.unlink(temporary)
  volumeMounts:
    - name: collection-config
      mountPath: /config-source
      readOnly: true
    - name: collection-transport
      mountPath: /transport-source
      readOnly: true
    - name: protected-files
      mountPath: /protected
{{- end }}

{{- define "troshka.nativeRuntime.env" -}}
{{- if .worker.cliImage }}
- name: PATH
  value: /opt/collection-bin:/opt/app-root/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
{{- end }}
{{- end }}

{{- define "troshka.nativeRuntime.volumeMounts" -}}
{{- if .worker.cliImage }}
- name: collection-bin
  mountPath: /opt/collection-bin
  readOnly: true
{{- end }}
- name: config
  mountPath: /opt/app-root/src/config/config.yaml
  subPath: config.yaml
  readOnly: true
- name: protected-files
  mountPath: /etc/troshka/collection
  subPath: config
  readOnly: true
- name: protected-files
  mountPath: /etc/troshka/collection-transport
  subPath: transport
  readOnly: true
{{- end }}

{{- define "troshka.nativeRuntime.volumes" -}}
{{- if .worker.cliImage }}
- name: collection-bin
  emptyDir: {}
{{- end }}
- name: protected-files
  emptyDir:
    medium: Memory
- name: config
  configMap:
    name: troshka-config
- name: collection-config
  configMap:
    name: {{ required "Collection worker requires protected configMapName" .worker.configMapName }}
- name: collection-transport
  secret:
    secretName: {{ required "Collection worker requires transport secretName" .worker.secretName }}
{{- end }}
