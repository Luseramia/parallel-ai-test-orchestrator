pipeline {
    agent {
        kubernetes {
            yaml """
apiVersion: v1
kind: Pod
metadata:
  annotations:
    vault.hashicorp.com/agent-inject: "true"
    vault.hashicorp.com/role: "kaniko"
    vault.hashicorp.com/template-config-exit-on-retry-failure: "true"
    vault.hashicorp.com/agent-inject-secret-ai-test-database-url: "dev-secrets/data/ai"
    vault.hashicorp.com/error-on-missing-key-ai-test-database-url: "true"
    vault.hashicorp.com/agent-inject-template-ai-test-database-url: |
      {{- with secret "dev-secrets/data/ai" -}}{{ .Data.data.AI_TEST_DATABASE_URL }}{{- end -}}
    vault.hashicorp.com/agent-inject-secret-ai-test-api-token: "dev-secrets/data/ai"
    vault.hashicorp.com/error-on-missing-key-ai-test-api-token: "true"
    vault.hashicorp.com/agent-inject-template-ai-test-api-token: |
      {{- with secret "dev-secrets/data/ai" -}}{{ .Data.data.AI_TEST_API_TOKEN }}{{- end -}}
    vault.hashicorp.com/agent-inject-secret-ai-test-codex-runner-token: "dev-secrets/data/ai"
    vault.hashicorp.com/error-on-missing-key-ai-test-codex-runner-token: "true"
    vault.hashicorp.com/agent-inject-template-ai-test-codex-runner-token: |
      {{- with secret "dev-secrets/data/ai" -}}{{ .Data.data.AI_TEST_CODEX_RUNNER_TOKEN }}{{- end -}}
    vault.hashicorp.com/agent-inject-secret-ai-test-test-runner-token: "dev-secrets/data/ai"
    vault.hashicorp.com/error-on-missing-key-ai-test-test-runner-token: "true"
    vault.hashicorp.com/agent-inject-template-ai-test-test-runner-token: |
      {{- with secret "dev-secrets/data/ai" -}}{{ .Data.data.AI_TEST_TEST_RUNNER_TOKEN }}{{- end -}}
    vault.hashicorp.com/agent-inject-secret-ai-test-artifact-signing-key: "dev-secrets/data/ai"
    vault.hashicorp.com/error-on-missing-key-ai-test-artifact-signing-key: "true"
    vault.hashicorp.com/agent-inject-template-ai-test-artifact-signing-key: |
      {{- with secret "dev-secrets/data/ai" -}}{{ .Data.data.AI_TEST_ARTIFACT_SIGNING_KEY }}{{- end -}}
    vault.hashicorp.com/agent-inject-secret-ai-test-completion-webhook-secret: "dev-secrets/data/ai"
    vault.hashicorp.com/error-on-missing-key-ai-test-completion-webhook-secret: "true"
    vault.hashicorp.com/agent-inject-template-ai-test-completion-webhook-secret: |
      {{- with secret "dev-secrets/data/ai" -}}{{ .Data.data.AI_TEST_COMPLETION_WEBHOOK_SECRET }}{{- end -}}
    vault.hashicorp.com/agent-inject-secret-ai-test-github-read-token: "dev-secrets/data/ai"
    vault.hashicorp.com/error-on-missing-key-ai-test-github-read-token: "true"
    vault.hashicorp.com/agent-inject-template-ai-test-github-read-token: |
      {{- with secret "dev-secrets/data/ai" -}}{{ .Data.data.AI_TEST_GITHUB_READ_TOKEN }}{{- end -}}
    vault.hashicorp.com/agent-inject-secret-ai-test-openai-api-key: "dev-secrets/data/ai"
    vault.hashicorp.com/error-on-missing-key-ai-test-openai-api-key: "true"
    vault.hashicorp.com/agent-inject-template-ai-test-openai-api-key: |
      {{- with secret "dev-secrets/data/ai" -}}{{ .Data.data.AI_TEST_OPENAI_API_KEY }}{{- end -}}
  labels:
    app.kubernetes.io/name: parallel-ai-test-orchestrator-ci
spec:
  serviceAccountName: kaniko
  automountServiceAccountToken: true
  securityContext:
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: helper
      image: alpine/git:2.49.1
      command: ["sleep"]
      args: ["99d"]
      tty: true
      volumeMounts:
        - name: ci-workspace
          mountPath: /ci-workspace

    - name: python
      image: python:3.13-bookworm
      command: ["sleep"]
      args: ["99d"]
      tty: true
      volumeMounts:
        - name: ci-workspace
          mountPath: /ci-workspace

    - name: node
      image: node:22-bookworm
      command: ["sleep"]
      args: ["99d"]
      tty: true
      volumeMounts:
        - name: ci-workspace
          mountPath: /ci-workspace

    - name: kaniko
      image: gcr.io/kaniko-project/executor:v1.23.2-debug
      command: ["sleep"]
      args: ["99d"]
      tty: true
      volumeMounts:
        - name: ci-workspace
          mountPath: /ci-workspace
        - name: docker-config
          mountPath: /kaniko/.docker

    - name: kubectl
      image: alpine/k8s:1.32.9
      command: ["sleep"]
      args: ["99d"]
      tty: true
      volumeMounts:
        - name: ci-workspace
          mountPath: /ci-workspace

  volumes:
    - name: ci-workspace
      emptyDir: {}
    - name: docker-config
      emptyDir: {}
"""
        }
    }

    options {
        skipDefaultCheckout(true)
        buildDiscarder(logRotator(numToKeepStr: '20'))
        disableConcurrentBuilds()
        timeout(time: 60, unit: 'MINUTES')
        timestamps()
    }

    environment {
        APP_GIT_REPO = 'git@github.com:Luseramia/parallel-ai-test-orchestrator.git'
        APP_GIT_BRANCH = 'main'
        DEPLOYMENT_GIT_REPO = 'git@github.com:Luseramia/k8s-project-helm.git'
        DEPLOYMENT_GIT_BRANCH = 'main'
        DEPLOYMENT_DIR = 'parallel-ai-test-orchestrator'

        REGISTRY = 'registry.registry.svc.cluster.local:5000'
        GATEWAY_IMAGE = 'parallel-ai-test-gateway'
        CODEX_RUNNER_IMAGE = 'parallel-ai-test-codex-runner'
        TEST_RUNNER_IMAGE = 'parallel-ai-test-runner'
        IMAGE_TAG = "${BUILD_NUMBER}"
        VAULT_SECRET_DIR = '/vault/secrets'

        N8N_WEBHOOK = 'http://n8n.n8n.svc.cluster.local:443/webhook/jenkins-notify'
    }

    stages {
        stage('Checkout source and deployment manifests') {
            steps {
                script {
                    safeRun('Checkout source and deployment manifests') {
                        withCredentials([sshUserPrivateKey(
                            credentialsId: 'github_key',
                            keyFileVariable: 'GITHUB_KEY'
                        )]) {
                            container('helper') {
                                sh '''
                                    set -eu

                                    rm -rf -- /ci-workspace/source /ci-workspace/deployment
                                    mkdir -p /ci-workspace/source /ci-workspace/deployment ~/.ssh
                                    chmod 700 ~/.ssh

                                    cat > ~/.ssh/config <<'SSHCFG'
Host github.com
    Hostname ssh.github.com
    Port 443
    StrictHostKeyChecking yes
SSHCFG
                                    ssh-keyscan -p 443 ssh.github.com > ~/.ssh/known_hosts 2>/dev/null
                                    chmod 600 ~/.ssh/config ~/.ssh/known_hosts
                                    export GIT_SSH_COMMAND="ssh -i ${GITHUB_KEY}"

                                    git clone --branch "${APP_GIT_BRANCH}" --single-branch \
                                      "${APP_GIT_REPO}" /ci-workspace/source
                                    git clone --branch "${DEPLOYMENT_GIT_BRANCH}" --single-branch \
                                      "${DEPLOYMENT_GIT_REPO}" /ci-workspace/deployment

                                    git config --global --add safe.directory /ci-workspace/source
                                    git config --global --add safe.directory /ci-workspace/deployment
                                    git -C /ci-workspace/source rev-parse HEAD
                                    git -C /ci-workspace/deployment rev-parse HEAD
                                '''
                            }
                        }
                    }
                }
            }
        }

        stage('Validate source layout') {
            steps {
                script {
                    safeRun('Validate source layout') {
                        container('helper') {
                            sh '''
                                set -eu
                                cd /ci-workspace/source

                                test -f gateway/Dockerfile
                                test -f codex-runner/Dockerfile
                                test -f test-runner/Dockerfile
                                test -f gateway/requirements-dev.lock
                                test -f codex-runner/package-lock.json
                                test -f k8s/base/kustomization.yaml
                                test -f contracts/test-plan.schema.json

                                test -z "$(git status --porcelain)"
                            '''
                        }
                    }
                }
            }
        }

        stage('Gateway and test-runner tests') {
            steps {
                script {
                    safeRun('Gateway and test-runner tests') {
                        container('python') {
                            sh '''
                                set -eu
                                python -m venv /tmp/ai-test-ci-venv
                                /tmp/ai-test-ci-venv/bin/python -m pip install \
                                  --disable-pip-version-check --no-cache-dir \
                                  -r /ci-workspace/source/gateway/requirements-dev.lock

                                cd /ci-workspace/source/gateway
                                /tmp/ai-test-ci-venv/bin/ruff check app tests
                                /tmp/ai-test-ci-venv/bin/python -m unittest discover -s tests -v

                                cd /ci-workspace/source/test-runner
                                /tmp/ai-test-ci-venv/bin/ruff check runner.py tests
                                /tmp/ai-test-ci-venv/bin/python -m unittest discover -s tests -v
                            '''
                        }
                    }
                }
            }
        }

        stage('Codex runner tests') {
            steps {
                script {
                    safeRun('Codex runner tests') {
                        container('node') {
                            sh '''
                                set -eu
                                cd /ci-workspace/source/codex-runner
                                npm ci --no-audit --no-fund
                                npm run typecheck
                                npm test
                                npm audit --omit=dev --audit-level=high
                            '''
                        }
                    }
                }
            }
        }

        stage('Render Kubernetes manifests') {
            steps {
                script {
                    safeRun('Render Kubernetes manifests') {
                        container('kubectl') {
                            sh '''
                                set -eu
                                kubectl kustomize /ci-workspace/source/k8s/base \
                                  > /tmp/parallel-ai-test-rendered.yaml
                                test -s /tmp/parallel-ai-test-rendered.yaml
                            '''
                        }
                    }
                }
            }
        }

        stage('Apply runtime secrets') {
            steps {
                script {
                    safeRun('Apply runtime secrets') {
                        container('kubectl') {
                            sh '''
                                set +x
                                set -eu

                                test -s "${VAULT_SECRET_DIR}/ai-test-database-url"
                                test -s "${VAULT_SECRET_DIR}/ai-test-api-token"
                                test -s "${VAULT_SECRET_DIR}/ai-test-codex-runner-token"
                                test -s "${VAULT_SECRET_DIR}/ai-test-test-runner-token"
                                test -s "${VAULT_SECRET_DIR}/ai-test-artifact-signing-key"
                                test -s "${VAULT_SECRET_DIR}/ai-test-completion-webhook-secret"
                                test -s "${VAULT_SECRET_DIR}/ai-test-github-read-token"
                                test -s "${VAULT_SECRET_DIR}/ai-test-openai-api-key"

                                # Argo CD owns namespace/RBAC bootstrap. A newly created
                                # application can need one reconciliation interval before
                                # Jenkins is allowed to create these external Secrets.
                                apply_secret_with_retry() {
                                  namespace="$1"
                                  secret_name="$2"
                                  shift 2
                                  attempt=1
                                  max_attempts=120
                                  error_file="/tmp/${secret_name}-apply.err"

                                  while ! kubectl -n "${namespace}" create secret generic "${secret_name}" "$@" \
                                    --dry-run=client -o yaml \
                                    | kubectl apply -f - >/dev/null 2>"${error_file}"
                                  do
                                    if [ "${attempt}" -eq 1 ] || [ $((attempt % 12)) -eq 0 ]; then
                                      echo "Waiting for ${namespace} and Jenkins Secret RBAC (${attempt}/${max_attempts})" >&2
                                      sed -n '1,5p' "${error_file}" >&2
                                    fi
                                    if [ "${attempt}" -ge "${max_attempts}" ]; then
                                      echo "Timed out waiting to apply ${namespace}/${secret_name}" >&2
                                      rm -f -- "${error_file}"
                                      return 1
                                    fi
                                    attempt=$((attempt + 1))
                                    sleep 5
                                  done

                                  rm -f -- "${error_file}"
                                  echo "Applied ${namespace}/${secret_name}"
                                }

                                apply_secret_with_retry ai-test-system ai-test-gateway \
                                  --from-file=database-url="${VAULT_SECRET_DIR}/ai-test-database-url" \
                                  --from-file=api-token="${VAULT_SECRET_DIR}/ai-test-api-token" \
                                  --from-file=codex-runner-token="${VAULT_SECRET_DIR}/ai-test-codex-runner-token" \
                                  --from-file=test-runner-token="${VAULT_SECRET_DIR}/ai-test-test-runner-token" \
                                  --from-file=artifact-signing-key="${VAULT_SECRET_DIR}/ai-test-artifact-signing-key" \
                                  --from-file=completion-webhook-secret="${VAULT_SECRET_DIR}/ai-test-completion-webhook-secret" \
                                  --from-file=github-read-token="${VAULT_SECRET_DIR}/ai-test-github-read-token"

                                apply_secret_with_retry ai-test-runners ai-test-codex-callback \
                                  --from-file=token="${VAULT_SECRET_DIR}/ai-test-codex-runner-token"

                                apply_secret_with_retry ai-test-runners ai-test-test-callback \
                                  --from-file=token="${VAULT_SECRET_DIR}/ai-test-test-runner-token"

                                apply_secret_with_retry ai-test-runners ai-test-codex-auth \
                                  --from-file=api-key="${VAULT_SECRET_DIR}/ai-test-openai-api-key"

                                kubectl -n ai-test-system get secret ai-test-gateway >/dev/null
                                kubectl -n ai-test-runners get secret ai-test-codex-callback >/dev/null
                                kubectl -n ai-test-runners get secret ai-test-test-callback >/dev/null
                                kubectl -n ai-test-runners get secret ai-test-codex-auth >/dev/null
                                echo 'Runtime Secrets applied from Vault'
                            '''
                        }
                    }
                }
            }
        }

        stage('Build and push images') {
            steps {
                script {
                    safeRun('Build and push images') {
                        container('kaniko') {
                            sh '''
                                set -eu
                                mkdir -p /ci-workspace/digests

                                build_image() {
                                    image_name="$1"
                                    dockerfile="$2"
                                    destination="${REGISTRY}/${image_name}:${IMAGE_TAG}"

                                    echo "Building ${destination}"
                                    /kaniko/executor \
                                      --context=/ci-workspace/source \
                                      --dockerfile="/ci-workspace/source/${dockerfile}" \
                                      --destination="${destination}" \
                                      --digest-file="/ci-workspace/digests/${image_name}.txt" \
                                      --cache=true \
                                      --cache-repo="${REGISTRY}/kaniko-cache/${image_name}" \
                                      --insecure \
                                      --skip-tls-verify \
                                      --snapshot-mode=redo
                                }

                                build_image "${GATEWAY_IMAGE}" gateway/Dockerfile
                                build_image "${CODEX_RUNNER_IMAGE}" codex-runner/Dockerfile
                                build_image "${TEST_RUNNER_IMAGE}" test-runner/Dockerfile
                            '''
                        }
                    }
                }
            }
        }

        stage('Update deployment image tags') {
            steps {
                script {
                    safeRun('Update deployment image tags') {
                        container('helper') {
                            sh '''
                                set -eu

                                target_dir="/ci-workspace/deployment/${DEPLOYMENT_DIR}"
                                test -f "${target_dir}/gateway.yaml"
                                test -f "${target_dir}/reconciler-cronjob.yaml"

                                gateway_ref="${REGISTRY}/${GATEWAY_IMAGE}:${IMAGE_TAG}"
                                codex_ref="${REGISTRY}/${CODEX_RUNNER_IMAGE}:${IMAGE_TAG}"
                                test_ref="${REGISTRY}/${TEST_RUNNER_IMAGE}:${IMAGE_TAG}"

                                sed -i -E \
                                  "s|image: [^[:space:]]*parallel-ai-test-gateway:[^[:space:]]*|image: ${gateway_ref}|g" \
                                  "${target_dir}/gateway.yaml" \
                                  "${target_dir}/reconciler-cronjob.yaml"
                                sed -i -E \
                                  "/name: CODEX_RUNNER_IMAGE/{n;s|value: [^[:space:]]+|value: ${codex_ref}|;}" \
                                  "${target_dir}/gateway.yaml"
                                sed -i -E \
                                  "/name: TEST_RUNNER_IMAGE/{n;s|value: [^[:space:]]+|value: ${test_ref}|;}" \
                                  "${target_dir}/gateway.yaml"

                                grep -F "image: ${gateway_ref}" "${target_dir}/gateway.yaml"
                                grep -F "image: ${gateway_ref}" "${target_dir}/reconciler-cronjob.yaml"
                                grep -F "value: ${codex_ref}" "${target_dir}/gateway.yaml"
                                grep -F "value: ${test_ref}" "${target_dir}/gateway.yaml"
                            '''
                        }

                        container('python') {
                            sh '''
                                set -eu
                                /tmp/ai-test-ci-venv/bin/python \
                                  /ci-workspace/source/scripts/validate-kubernetes-manifests.py \
                                  "/ci-workspace/deployment/${DEPLOYMENT_DIR}" \
                                  --exclude argocd-app.yaml
                            '''
                        }

                        withCredentials([sshUserPrivateKey(
                            credentialsId: 'github_key',
                            keyFileVariable: 'GITHUB_KEY'
                        )]) {
                            container('helper') {
                                sh '''
                                    set -eu
                                    cd /ci-workspace/deployment
                                    git config --global --add safe.directory /ci-workspace/deployment
                                    git config user.email 'jenkins@ci.local'
                                    git config user.name 'Jenkins CI'
                                    git add -- "${DEPLOYMENT_DIR}"

                                    if git diff --cached --quiet; then
                                      echo 'No deployment image changes to commit'
                                    else
                                      git commit -m "Deploy parallel-ai-test images ${IMAGE_TAG} [skip ci]"
                                      export GIT_SSH_COMMAND="ssh -i ${GITHUB_KEY}"
                                      git push origin "${DEPLOYMENT_GIT_BRANCH}"
                                    fi
                                '''
                            }
                        }
                    }
                }
            }
        }

    }

    post {
        success {
            script {
                notifyN8n(
                    stageName: 'Pipeline',
                    status: 'success',
                    message: "Published gateway, Codex runner, and test runner tag ${env.IMAGE_TAG}; Argo CD will sync it automatically"
                )
            }
        }
        failure {
            script {
                notifyN8n(
                    stageName: env.FAILED_STAGE ?: 'unknown',
                    status: 'failed',
                    message: env.FAILED_REASON ?: 'See Jenkins console log'
                )
            }
        }
    }
}


def safeRun(String stageName, Closure body) {
    try {
        body()
    } catch (err) {
        env.FAILED_STAGE = stageName
        env.FAILED_REASON = (err.getMessage() ?: 'no message').take(1000)
        throw err
    }
}


def notifyN8n(Map params) {
    def stageName = (params.stageName ?: 'unknown').toString().take(200)
    def status = (params.status ?: 'info').toString().take(50)
    def message = (params.message ?: '').toString().take(1800)

    try {
        container('helper') {
            withEnv([
                "NOTIFY_STAGE=${stageName}",
                "NOTIFY_STATUS=${status}",
                "NOTIFY_MESSAGE=${message}"
            ]) {
                sh '''
                    set +x
                    command -v curl >/dev/null 2>&1 || apk add --no-cache curl >/dev/null 2>&1 || true
                    command -v jq >/dev/null 2>&1 || apk add --no-cache jq >/dev/null 2>&1 || true
                    if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
                      echo 'n8n notification tools are unavailable (non-fatal)'
                      exit 0
                    fi

                    jq -n \
                      --arg job "${JOB_NAME}" \
                      --arg build "${BUILD_NUMBER}" \
                      --arg url "${BUILD_URL}" \
                      --arg stage "${NOTIFY_STAGE}" \
                      --arg status "${NOTIFY_STATUS}" \
                      --arg message "${NOTIFY_MESSAGE}" \
                      '{job:$job,build:$build,url:$url,stage:$stage,status:$status,message:$message}' \
                      > /tmp/n8n-payload.json

                    curl -fsS -X POST "${N8N_WEBHOOK}" \
                      -H 'Content-Type: application/json' \
                      --data @/tmp/n8n-payload.json >/dev/null || \
                      echo 'n8n notification failed (non-fatal)'
                    rm -f -- /tmp/n8n-payload.json
                '''
            }
        }
    } catch (err) {
        echo "n8n notification skipped because the Jenkins agent is unavailable: ${err.getMessage()}"
    }
}
