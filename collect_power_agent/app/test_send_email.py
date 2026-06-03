from app.mail_sender import send_email

send_email(
    to_email="rthitame@gmail.com",
    subject="BlueBoot SMTP Test",
    text_body="""
Hello,

This is a plain text test email from BlueBoot.

Regards,
BlueBoot AI
""",
    html_body="""
<html>
<body>
    <h2>BlueBoot SMTP Test</h2>

    <p>Hello,</p>

    <p>This is an <b>HTML test email</b> from BlueBoot.</p>

    <p>Regards,<br>BlueBoot AI</p>
</body>
</html>
"""
)
