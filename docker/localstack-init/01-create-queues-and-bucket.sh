#!/bin/sh
# WHY THIS FILE EXISTS
#     The queue, its dead-letter queue, and the report bucket that Phase 3 will use. LocalStack
#     runs everything in `/etc/localstack/init/ready.d` once it answers, so this is the
#     bootstrap `docker compose up --wait` is really waiting for.
#
#     **Everything here is consumed now.** `python -m worker` receives from the queue and
#     dead-letters into the DLQ (step 20); the export node writes `reports/{job_id}.json` into the
#     bucket and the API presigns it (step 22a). The queue's shape is declared once - in
#     docker-compose.yml, which passes it in.
#
#     **It converges rather than creates.** Each resource is checked, created if absent, and
#     then has its attributes set unconditionally, so running it twice leaves exactly the same
#     infrastructure and a changed setting in docker-compose.yml actually lands.
set -eu

QUEUE_NAME="${JOBS_QUEUE_NAME:?JOBS_QUEUE_NAME is not set}"
DLQ_NAME="${JOBS_DLQ_NAME:?JOBS_DLQ_NAME is not set}"
VISIBILITY_TIMEOUT="${JOBS_QUEUE_VISIBILITY_TIMEOUT:?JOBS_QUEUE_VISIBILITY_TIMEOUT is not set}"
MAX_RECEIVE_COUNT="${JOBS_QUEUE_MAX_RECEIVE_COUNT:?JOBS_QUEUE_MAX_RECEIVE_COUNT is not set}"
BUCKET="${REPORTS_BUCKET:?REPORTS_BUCKET is not set}"
REGION="${AWS_DEFAULT_REGION:?AWS_DEFAULT_REGION is not set}"

queue_url() {
    awslocal sqs get-queue-url --queue-name "$1" --query QueueUrl --output text
}

# FIFO preserves one job's message order (ADR 0010 decision 4), and it can only be set when the
# queue is created. ADR 0016's PostgreSQL execution lock is the same-job writer fence if an expired
# delivery overlaps its redelivery. This remains create-if-absent rather than create-and-ignore.
ensure_fifo_queue() {
    if ! queue_url "$1" >/dev/null 2>&1; then
        awslocal sqs create-queue --queue-name "$1" --attributes FifoQueue=true >/dev/null
        echo "created queue $1"
    fi
}

ensure_fifo_queue "$DLQ_NAME"
ensure_fifo_queue "$QUEUE_NAME"

DLQ_ARN=$(awslocal sqs get-queue-attributes \
    --queue-url "$(queue_url "$DLQ_NAME")" \
    --attribute-names QueueArn --query Attributes.QueueArn --output text)

# Set every time, so the numbers in docker-compose.yml are the ones the queue actually has.
awslocal sqs set-queue-attributes \
    --queue-url "$(queue_url "$QUEUE_NAME")" \
    --attributes "{
        \"VisibilityTimeout\": \"${VISIBILITY_TIMEOUT}\",
        \"RedrivePolicy\": \"{\\\"deadLetterTargetArn\\\":\\\"${DLQ_ARN}\\\",\\\"maxReceiveCount\\\":\\\"${MAX_RECEIVE_COUNT}\\\"}\"
    }"
echo "queue $QUEUE_NAME: visibility ${VISIBILITY_TIMEOUT}s, ${MAX_RECEIVE_COUNT} deliveries then $DLQ_NAME"

if ! awslocal s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    # Every region except us-east-1 requires the location constraint, and this one follows
    # AWS_REGION, whose default is ap-south-1.
    awslocal s3api create-bucket --bucket "$BUCKET" \
        --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
    echo "created bucket $BUCKET"
fi
