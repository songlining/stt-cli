# Homebrew cask scaffold for stt.
#
# Replace url/sha256/version with values from a real Developer-ID-signed,
# notarized, stapled release archive before publishing to a tap.

cask "stt" do
  version "0.1.0"
  sha256 "REPLACE_WITH_RELEASE_SHA256"

  url "https://example.com/releases/stt-#{version}-macos.zip"
  name "stt"
  desc "macOS speech-to-text CLI with bundled app wrapper for TCC attribution"
  homepage "https://example.com/stt"

  depends_on macos: ">= :sonoma"

  app "stt.app"

  zap trash: [
    "~/Library/Application Support/stt",
    "~/Library/Caches/stt",
  ]
end
