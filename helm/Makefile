# SeaweedFS release wrapper — every mutation goes through a pinned chart version.
#
# The chart version is pinned here for a reason: `helm upgrade` without `--version`
# took 4.49.0, which pins an image tag that was never published (404), and all six
# pods went ImagePullBackOff. Values cannot express the chart version, so the pin lives here.
#
#   make status          release state, pods, volume slots
#   make drift           is the cluster still what seaweedfs-values.yaml says?
#   make diff            same comparison, with a line diff for each resource
#   make upgrade         helm upgrade --install, pinned, atomic, waits
#   make rollback        roll back to the previous revision
#   make values          show the values stored in the release
#   make lint            render + helm lint, no cluster change
#   make docs            is ~/lab/SEQUENCE-FOR-INSTALL.md still in step with
#                        docs/rebuild-checklist.md?
#
# Override anything on the command line:
#   make upgrade RELEASE=seaweedfs NAMESPACE=s3 CHART=seaweedfs/seaweedfs VERSION=4.48.0

RELEASE   ?= seaweedfs
NAMESPACE ?= s3
CHART     ?= seaweedfs/seaweedfs
VERSION   ?= 4.48.0
VALUES    ?= seaweedfs-values.yaml
TIMEOUT   ?= 4m

# kubectl/helm read KUBECONFIG from the environment; default to the usual location.
KUBECONFIG ?= $(HOME)/.kube/config
export KUBECONFIG

HELM := helm -n $(NAMESPACE)
BASE := $(HELM) upgrade --install $(RELEASE) $(CHART) --version $(VERSION) -f $(VALUES)

.PHONY: help status drift diff upgrade install rollback values lint pods slots volumes

help:
	@sed -n 's/^#   //p' Makefile

status:
	@$(HELM) list | grep -E 'NAME|$(RELEASE)'
	@echo
	@$(HELM) history $(RELEASE) --max 5
	@echo
	@kubectl -n $(NAMESPACE) get pods --no-headers | \
	  awk '{printf "  %-38s %-4s %-12s restarts=%s\n", $$1, $$2, $$3, $$4}'

pods:
	@kubectl -n $(NAMESPACE) get pods -o wide

slots:
	@kubectl -n $(NAMESPACE) exec $(RELEASE)-master-0 -- \
	  wget -qO- http://127.0.0.1:9333/dir/status | python3 ./slots.py

volumes:
	@$(MAKE) --no-print-directory slots

drift:
	@python3 ./drift.py --release $(RELEASE) --namespace $(NAMESPACE) \
	  --chart $(CHART) --version $(VERSION) --values $(VALUES)

docs:
	@python3 ./check-doc-parity.py


lint:
	@$(HELM) template $(RELEASE) $(CHART) --version $(VERSION) -f $(VALUES) > /dev/null && \
	  echo "  template: ok ($(CHART) $(VERSION))"
	@# helm 4 cannot lint a repo/chart reference directly, so pull that exact version
	@tmp=$$(mktemp -d); \
	  helm pull $(CHART) --version $(VERSION) --untar --untardir $$tmp > /dev/null && \
	  helm lint $$tmp/$(notdir $(CHART)) -f $(VALUES); \
	  rc=$$?; rm -rf $$tmp; exit $$rc

diff:
	@python3 ./drift.py --release $(RELEASE) --namespace $(NAMESPACE) \
	  --chart $(CHART) --version $(VERSION) --values $(VALUES) --verbose

upgrade:
	@echo "  upgrading $(RELEASE) to $(CHART) $(VERSION) (atomic, wait $(TIMEOUT))"
	$(BASE) --atomic --wait --timeout $(TIMEOUT)

install: upgrade

rollback:
	@$(HELM) rollback $(RELEASE) --wait --timeout $(TIMEOUT)
	@$(HELM) history $(RELEASE) --max 3

values:
	@$(HELM) get values $(RELEASE) -o yaml
