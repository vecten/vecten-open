mock_provider "aws" {}

variables {
  identifier            = "acme-prod-warehouse"
  environment           = "prod"
  destroying            = false
  instance_class        = "db.t4g.medium"
  allocated_storage     = 20
  max_allocated_storage = 100
  private_subnet_ids    = ["subnet-private"]
  public_subnet_ids     = ["subnet-public"]
  security_group_id     = "sg-123"
  public_cidrs          = []
}

run "managed_master_secret_is_the_output" {
  module {
    source = "./modules/postgres"
  }

  override_resource {
    target = aws_db_instance.this
    values = {
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:rds!db-1" }]
    }
  }

  assert {
    condition     = output.master_secret_arn == "arn:aws:secretsmanager:eu-west-1:123456789012:secret:rds!db-1"
    error_message = "the output is the ARN of the RDS-managed master secret"
  }
}

run "restored_instance_without_a_managed_secret_is_refused" {
  state_key = "restored"

  module {
    source = "./modules/postgres"
  }

  override_resource {
    target = aws_db_instance.this
    values = {
      master_user_secret = []
    }
  }

  expect_failures = [output.master_secret_arn]
}
