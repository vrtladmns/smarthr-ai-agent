#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run with sudo:"
  echo "  sudo ./scripts/install_mssql_odbc_ubuntu.sh"
  exit 1
fi

. /etc/os-release

# Remove an older/bad source file before `apt-get update`, otherwise apt can fail
# before this script gets a chance to repair it.
rm -f /etc/apt/sources.list.d/mssql-release.list

apt-get update
apt-get install -y --no-install-recommends curl gnupg ca-certificates unixodbc

curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
  | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg

repo_os="${ID}"
repo_version="${VERSION_ID}"
repo_codename="${VERSION_CODENAME}"

if [[ "${ID}" == "ubuntu" ]]; then
  case "${VERSION_ID}" in
    "24.04")
      repo_version="24.04"
      repo_codename="noble"
      ;;
    "22.04")
      repo_version="22.04"
      repo_codename="jammy"
      ;;
    "20.04")
      repo_version="20.04"
      repo_codename="focal"
      ;;
    *)
      echo "Microsoft has no SQL Server ODBC repo for Ubuntu ${VERSION_ID} (${VERSION_CODENAME}) yet."
      echo "Using Microsoft Ubuntu 24.04 (noble) repo as a compatibility fallback."
      repo_version="24.04"
      repo_codename="noble"
      ;;
  esac
elif [[ "${ID}" == "debian" ]]; then
  case "${VERSION_ID}" in
    "12")
      repo_version="12"
      repo_codename="bookworm"
      ;;
    "11")
      repo_version="11"
      repo_codename="bullseye"
      ;;
    *)
      echo "Microsoft has no SQL Server ODBC repo mapping in this script for Debian ${VERSION_ID} (${VERSION_CODENAME})."
      echo "Using Microsoft Debian 12 (bookworm) repo as a compatibility fallback."
      repo_version="12"
      repo_codename="bookworm"
      ;;
  esac
else
  echo "Unsupported OS ID '${ID}'. This script supports Ubuntu/Debian style apt systems."
  exit 1
fi

echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/${repo_os}/${repo_version}/prod ${repo_codename} main" \
  > /etc/apt/sources.list.d/mssql-release.list

apt-get update
ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18

echo "Microsoft ODBC Driver 18 for SQL Server installed."
