terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 3.48.0"
    }
  }
  required_version = "~> 1.0"
}

variable region {
 default =  "eu-west-1"
}

provider "aws" {
  region = var.region
}

data aws_caller_identity current {}

locals {
 prefix = "flesk"
 account_id          = data.aws_caller_identity.current.account_id
 ecr_repository_name = "${local.prefix}-lambda-container"
 ecr_image_tag       = "latest"
}

resource aws_ecr_repository repo {
 name = local.ecr_repository_name
}

resource null_resource ecr_image {
 triggers = {
   python_file = md5(file("${path.module}/app/main.py"))
   docker_file = md5(file("${path.module}/app/Dockerfile"))
 }

 provisioner "local-exec" {
   command = <<EOF
           aws ecr get-login-password --region ${var.region} | docker login --username AWS --password-stdin ${local.account_id}.dkr.ecr.${var.region}.amazonaws.com
           cd ${path.module}/app
           docker build -t ${aws_ecr_repository.repo.repository_url}:${local.ecr_image_tag} .
           docker push ${aws_ecr_repository.repo.repository_url}:${local.ecr_image_tag}
       EOF
 }
}

data aws_ecr_image lambda_image {
 depends_on = [
   null_resource.ecr_image
 ]
 repository_name = local.ecr_repository_name
 image_tag       = local.ecr_image_tag
}

resource "aws_dynamodb_table" "flesk-dynamodb-table" {
  name           = "river_levels"
  billing_mode   = "PROVISIONED"
  read_capacity  = 5
  write_capacity = 5
  hash_key       = "river_name"
  range_key = "timestamp"

  attribute {
    name = "river_name"
    type = "S"
  }

    attribute {
    name = "timestamp"
    type = "N"
  }
}

resource "aws_lambda_function" "lambda_update_levels_table" {
  function_name = "lambda_update_levels_table"
  role          = aws_iam_role.iam_for_lambda.arn
  depends_on = [null_resource.ecr_image]
  timeout = 30
  image_uri = "${aws_ecr_repository.repo.repository_url}@${data.aws_ecr_image.lambda_image.id}"
  package_type = "Image"
}

output "lambda_name" {
 value = aws_lambda_function.lambda_update_levels_table.id
}

resource "aws_iam_role" "iam_for_lambda" {
  name = "iam_for_lambda_update_levels_table"

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Action": "sts:AssumeRole",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Effect": "Allow",
      "Sid": ""
    }
  ]
}
EOF
}

resource "aws_iam_role_policy" "iam_for_dynamo_write" {
  name = "iam_for_lambda_update_levels_table_dyanmo"
  role = aws_iam_role.iam_for_lambda.id
  policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement":[{
    "Effect": "Allow",
    "Action": [
     "dynamodb:BatchGetItem",
     "dynamodb:GetItem",
     "dynamodb:Query",
     "dynamodb:Scan",
     "dynamodb:BatchWriteItem",
     "dynamodb:PutItem",
     "dynamodb:UpdateItem"
    ],
    "Resource": "${aws_dynamodb_table.flesk-dynamodb-table.arn}"
   }
  ]
}
EOF
}

resource "aws_ecr_lifecycle_policy" "only_lastest_image_policy" {
  repository = aws_ecr_repository.repo.id

  policy = <<EOF
{
    "rules": [
        {
            "rulePriority": 1,
            "description": "Rule 1",
            "selection": {
                "tagStatus": "any",
                "countType": "imageCountMoreThan",
                "countNumber": 1
            },
            "action": {
                "type": "expire"
            }
        }
    ]
}
EOF
}

resource "aws_cloudwatch_event_rule" "every_five_minutes" {
    name = "every-five-minutes"
    description = "Fires every five minutes"
    schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_rule" "every_sixty_minutes" {
    name = "every_sixty_minutes"
    description = "Fires every every_sixty_minutes"
    schedule_expression = "rate(60 minutes)"
}

resource "aws_cloudwatch_event_target" "check_level_every_five_minutes" {
    rule = aws_cloudwatch_event_rule.every_five_minutes.name
    target_id = aws_lambda_function.lambda_update_levels_table.id
    arn = aws_lambda_function.lambda_update_levels_table.arn
    input     = "{\"current\":[\"current\"]}"
}

resource "aws_cloudwatch_event_target" "update_past_level_every_hour" {
    rule = aws_cloudwatch_event_rule.every_five_minutes.name
    target_id = aws_lambda_function.lambda_update_levels_table.id
    arn = aws_lambda_function.lambda_update_levels_table.arn
    input     = "{\"past\":[\"past\"]}"
}

resource "aws_lambda_permission" "allow_cloudwatch_to_call_lambda" {
    statement_id = "AllowExecutionFromCloudWatch"
    action = "lambda:InvokeFunction"
    function_name = aws_lambda_function.lambda_update_levels_table.id
    principal = "events.amazonaws.com"
    source_arn = aws_cloudwatch_event_rule.every_five_minutes.arn
}