package com.ismilebogra.alexa;

import android.app.Activity;
import android.os.Bundle;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceError;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Button;
import android.graphics.Color;

public class MainActivity extends Activity {
    private WebView webView;
    private LinearLayout root;
    private final String serverUrl = "http://127.0.0.1:8765";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.WHITE);

        webView = new WebView(this);
        webView.getSettings().setJavaScriptEnabled(true);
        webView.getSettings().setDomStorageEnabled(true);
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(
                    WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                return !url.startsWith(serverUrl);
            }

            @Override
            public void onReceivedError(
                    WebView view, WebResourceRequest request,
                    WebResourceError error) {
                if (request.isForMainFrame()) {
                    showStartScreen();
                }
            }
        });

        setContentView(root);
        showStartScreen();
    }

    private void showStartScreen() {
        root.removeAllViews();

        TextView title = new TextView(this);
        title.setText("Alexa Assistant");
        title.setTextSize(27);
        title.setTextColor(Color.BLACK);
        title.setPadding(24, 50, 24, 20);
        root.addView(title);

        TextView instructions = new TextView(this);
        instructions.setText(
            "প্রথমে Termux-এ Alexa সার্ভার চালু করুন।\n\n" +
            "cd ~/Alexa\n" +
            "python alexa.py\n\n" +
            "সার্ভার চালু হলে নিচের Open Alexa বাটনে চাপুন।"
        );
        instructions.setTextSize(17);
        instructions.setTextColor(Color.DKGRAY);
        instructions.setPadding(24, 10, 24, 25);
        root.addView(instructions);

        Button open = new Button(this);
        open.setText("Open Alexa");
        root.addView(open);

        open.setOnClickListener(v -> {
            root.removeAllViews();
            root.addView(webView, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            ));
            webView.loadUrl(serverUrl);
        });
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
