#!/bin/sh
set -eu

work_dir=/tmp/validation
mkdir -p "${work_dir}"
cat > "${work_dir}/main.tf"
cd "${work_dir}"

export TF_DATA_DIR="${work_dir}/.terraform"
if ! terraform init \
  -backend=false \
  -input=false \
  -no-color \
  -plugin-dir=/opt/providers > /tmp/terraform-init.log 2>&1; then
  cat /tmp/terraform-init.log >&2
  exit 125
fi

terraform validate -json
