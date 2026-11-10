terraform {
  required_version = "~> 1.16.0"

  required_providers {
    scaleway = {
      source  = "scaleway/scaleway"
      version = "~> 2.87.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9.0"
    }
  }
}
