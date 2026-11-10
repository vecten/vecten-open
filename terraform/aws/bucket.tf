resource "aws_s3_bucket" "artifacts" {
  bucket = local.artifacts_bucket
  # Only vecten destroy sets destroying, after it has snapshotted the databases.
  force_destroy = var.destroying

  lifecycle {
    precondition {
      condition     = length(local.artifacts_project_part) > 0 && length(local.artifacts_bucket) <= 63
      error_message = "The environment name leaves no room for the project in a 63-character bucket name; use a shorter environment name."
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-noncurrent-bundles"
    status = "Enabled"

    filter {
      prefix = "bundles/"
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.artifacts]
}
