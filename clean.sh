BUCKET="red-nwtx-athena"

while true; do
  OBJECTS=$(aws s3api list-object-versions \
    --bucket "$BUCKET" \
    --output json |
    jq '[
      (.Versions[]? | {Key: .Key, VersionId: .VersionId}),
      (.DeleteMarkers[]? | {Key: .Key, VersionId: .VersionId})
    ] | {Objects: ., Quiet: true}')

  COUNT=$(echo "$OBJECTS" | jq '.Objects | length')

  if [ "$COUNT" -eq 0 ]; then
    break
  fi

  echo "Deleting $COUNT versions/delete markers..."

  echo "$OBJECTS" |
    aws s3api delete-objects \
      --bucket "$BUCKET" \
      --delete file:///dev/stdin
done

aws s3api delete-bucket --bucket "$BUCKET"