resource "aws_security_group_rule" "service_https" {
  type              = "ingress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = ["10.20.0.0/16"]
  security_group_id = "sg-demo123"
}
