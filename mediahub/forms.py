from django import forms
from django.contrib.auth.forms import AuthenticationForm


class SharedLoginForm(AuthenticationForm):
    username = forms.CharField(
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Shared username", "autofocus": True})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Password"})
    )


class ClipCreateForm(forms.Form):
    requested_start = forms.FloatField(min_value=0, widget=forms.NumberInput(attrs={"class": "form-control"}))
    requested_end = forms.FloatField(min_value=0, widget=forms.NumberInput(attrs={"class": "form-control"}))
    title = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional clip title"}),
    )

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("requested_start")
        end = cleaned.get("requested_end")
        if start is not None and end is not None and end <= start:
            raise forms.ValidationError("Clip end time must be greater than start time.")
        return cleaned
