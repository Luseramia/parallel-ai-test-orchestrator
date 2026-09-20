pipeline {
    agent {
        kubernetes {
            yaml """
apiVersion: v1
kind: Pod
metadata:
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
        GITOPS_GIT_REPO = 'git@github.com:Luseramia/k8s-project-helm.git'
        GITOPS_GIT_BRANCH = 'main'
        GITOPS_DIR = 'parallel-ai-test-orchestrator'

        REGISTRY = 'registry.registry.svc.cluster.local:5000'
        GATEWAY_IMAGE = 'parallel-ai-test-gateway'
        CODEX_RUNNER_IMAGE = 'parallel-ai-test-codex-runner'
        TEST_RUNNER_IMAGE = 'parallel-ai-test-runner'
        IMAGE_TAG = "${BUILD_NUMBER}"

        ARGOCD_SERVER = 'https://argocd-server.argocd.svc.cluster.local'
        ARGOCD_APP = 'parallel-ai-test-orchestrator'
        N8N_WEBHOOK = 'http://n8n.n8n.svc.cluster.local:443/webhook/jenkins-notify'
    }

    stages {
        stage('Checkout source and GitOps') {
            steps {
                script {
                    safeRun('Checkout source and GitOps') {
                        withCredentials([sshUserPrivateKey(
                            credentialsId: 'github_key',
                            keyFileVariable: 'GITHUB_KEY'
                        )]) {
                            container('helper') {
                                sh '''
                                    set -eu

                                    rm -rf -- /ci-workspace/source /ci-workspace/gitops
                                    mkdir -p /ci-workspace/source /ci-workspace/gitops ~/.ssh
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
                                    git clone --branch "${GITOPS_GIT_BRANCH}" --single-branch \
                                      "${GITOPS_GIT_REPO}" /ci-workspace/gitops

                                    git config --global --add safe.directory /ci-workspace/source
                                    git config --global --add safe.directory /ci-workspace/gitops
                                    git -C /ci-workspace/source rev-parse HEAD
                                    git -C /ci-workspace/gitops rev-parse HEAD
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

        stage('Update GitOps manifests') {
            steps {
                script {
                    safeRun('Update GitOps manifests') {
                        container('helper') {
                            sh '''
                                set -eu

                                source_dir=/ci-workspace/source/k8s
                                target_dir="/ci-workspace/gitops/${GITOPS_DIR}"
                                rm -rf -- "${target_dir}/base" "${target_dir}/templates"
                                mkdir -p "${target_dir}"
                                cp -R "${source_dir}/base" "${target_dir}/base"
                                cp -R "${source_dir}/templates" "${target_dir}/templates"
                                cp "${source_dir}/README.md" "${target_dir}/README.md"

                                gateway_ref="${REGISTRY}/${GATEWAY_IMAGE}:${IMAGE_TAG}"
                                codex_ref="${REGISTRY}/${CODEX_RUNNER_IMAGE}:${IMAGE_TAG}"
                                test_ref="${REGISTRY}/${TEST_RUNNER_IMAGE}:${IMAGE_TAG}"

                                sed -i \
                                  "s|image: parallel-ai-test-gateway:latest|image: ${gateway_ref}|g" \
                                  "${target_dir}/base/gateway.yaml" \
                                  "${target_dir}/base/reconciler-cronjob.yaml"
                                sed -i \
                                  "s|value: parallel-ai-test-codex-runner:latest|value: ${codex_ref}|g" \
                                  "${target_dir}/base/gateway.yaml"
                                sed -i \
                                  "s|value: parallel-ai-test-runner:latest|value: ${test_ref}|g" \
                                  "${target_dir}/base/gateway.yaml"

                                grep -F "image: ${gateway_ref}" "${target_dir}/base/gateway.yaml"
                                grep -F "image: ${gateway_ref}" "${target_dir}/base/reconciler-cronjob.yaml"
                                grep -F "value: ${codex_ref}" "${target_dir}/base/gateway.yaml"
                                grep -F "value: ${test_ref}" "${target_dir}/base/gateway.yaml"
                            '''
                        }

                        container('kubectl') {
                            sh '''
                                set -eu
                                kubectl kustomize \
                                  "/ci-workspace/gitops/${GITOPS_DIR}/base" \
                                  > /tmp/parallel-ai-test-gitops-rendered.yaml
                                test -s /tmp/parallel-ai-test-gitops-rendered.yaml
                            '''
                        }

                        withCredentials([sshUserPrivateKey(
                            credentialsId: 'github_key',
                            keyFileVariable: 'GITHUB_KEY'
                        )]) {
                            container('helper') {
                                sh '''
                                    set -eu
                                    cd /ci-workspace/gitops
                                    git config --global --add safe.directory /ci-workspace/gitops
                                    git config user.email 'jenkins@ci.local'
                                    git config user.name 'Jenkins CI'
                                    git add -- "${GITOPS_DIR}"

                                    if git diff --cached --quiet; then
                                      echo 'No GitOps changes to commit'
                                    else
                                      git commit -m "Deploy parallel-ai-test images ${IMAGE_TAG} [skip ci]"
                                      export GIT_SSH_COMMAND="ssh -i ${GITHUB_KEY}"
                                      git push origin "${GITOPS_GIT_BRANCH}"
                                    fi
                                '''
                            }
                        }
                    }
                }
            }
        }

        stage('Trigger Argo CD') {
            steps {
                script {
                    safeRun('Trigger Argo CD') {
                        withCredentials([string(
                            credentialsId: 'argocd_token',
                            variable: 'ARGOCD_TOKEN'
                        )]) {
                            container('helper') {
                                sh '''
                                    set -eu
                                    command -v curl >/dev/null 2>&1 || apk add --no-cache curl >/dev/null

                                    set +x
                                    curl -fsSk -X POST \
                                      -H "Authorization: Bearer ${ARGOCD_TOKEN}" \
                                      -H 'Content-Type: application/json' \
                                      "${ARGOCD_SERVER}/api/v1/applications/${ARGOCD_APP}/sync" \
                                      -d '{}'
                                    set -x
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
                    message: "Built and deployed gateway, Codex runner, and test runner tag ${env.IMAGE_TAG}"
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
        always {
            container('helper') {
                sh 'rm -f -- /tmp/n8n-payload.json || true'
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
}
