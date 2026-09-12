param(
    [Parameter(Mandatory = $true)][string]$EverydaySheet,
    [Parameter(Mandatory = $true)][string]$BookingSheet,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

function Save-Tile {
    param(
        [System.Drawing.Bitmap]$Source,
        [int]$Left,
        [int]$Top,
        [int]$Right,
        [int]$Bottom,
        [string]$Name,
        [int]$Padding = 4
    )
    $x = [Math]::Max(0, $Left + $Padding)
    $y = [Math]::Max(0, $Top + $Padding)
    $width = [Math]::Max(1, [Math]::Min($Source.Width, $Right - $Padding) - $x)
    $height = [Math]::Max(1, [Math]::Min($Source.Height, $Bottom - $Padding) - $y)
    $rect = [System.Drawing.Rectangle]::new($x, $y, $width, $height)
    $tile = $Source.Clone($rect, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    try {
        $path = Join-Path $OutputDirectory ($Name + '.png')
        $tile.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $tile.Dispose()
    }
}

function Split-EqualRow {
    param(
        [System.Drawing.Bitmap]$Source,
        [int]$Top,
        [int]$Bottom,
        [string[]]$Names
    )
    for ($i = 0; $i -lt $Names.Count; $i++) {
        $left = [Math]::Round($Source.Width * $i / $Names.Count)
        $right = [Math]::Round($Source.Width * ($i + 1) / $Names.Count)
        Save-Tile -Source $Source -Left $left -Top $Top -Right $right -Bottom $Bottom -Name $Names[$i]
    }
}

$everydayRows = @(
    @('daily_hello','daily_thanks','daily_hard_work','daily_cling','daily_waiting','daily_no_more','daily_hmph','daily_cry'),
    @('daily_are_you_there','daily_okay','daily_love_you','daily_sleepy','daily_goodnight','daily_morning','daily_question','daily_wow'),
    @('daily_miss_you','daily_secret','daily_cheers','daily_please','daily_sorry','daily_hug','daily_ok','daily_no'),
    @('daily_angry','daily_aggrieved','daily_confused','daily_hehe','daily_heartbeat','daily_like','daily_shy','daily_peek'),
    @('daily_working','daily_eating','daily_on_the_way','daily_bathing','daily_buy','daily_congrats','daily_tired','daily_forever')
)
$everydayY = @(0, 180, 361, 543, 725, 909)
$everyday = [System.Drawing.Bitmap]::new($EverydaySheet)
try {
    for ($row = 0; $row -lt $everydayRows.Count; $row++) {
        Split-EqualRow -Source $everyday -Top $everydayY[$row] -Bottom $everydayY[$row + 1] -Names $everydayRows[$row]
    }
}
finally {
    $everyday.Dispose()
}

$booking = [System.Drawing.Bitmap]::new($BookingSheet)
try {
    Split-EqualRow -Source $booking -Top 0 -Bottom 199 -Names @(
        'book_hello','book_welcome','book_want_to_book','book_which_time','book_check_space','book_choose_date','book_available','book_reserved'
    )
    Split-EqualRow -Source $booking -Top 199 -Bottom 399 -Names @(
        'book_thanks','book_please','book_ok','book_no_problem','book_sorry','book_full','book_confirm_again','book_change_time','book_checking'
    )
    Split-EqualRow -Source $booking -Top 399 -Bottom 599 -Names @(
        'book_need_room','book_package','book_first_discount','book_choose_girl','book_all_right','book_same_day','book_late_notice','book_phone'
    )
    Split-EqualRow -Source $booking -Top 599 -Bottom 799 -Names @(
        'book_see_you','book_kiss','book_hard_work','book_more_questions','book_contact_anytime','book_leave_to_me','book_next_time','book_goodnight'
    )
    $lastNames = @('book_complete','book_fill_details','book_change_or_cancel','book_waiting_for_you','book_any_needs','book_peek')
    $lastCells = @(
        @(0,192), @(192,384), @(384,576), @(960,1152), @(1152,1344), @(1344,1536)
    )
    for ($i = 0; $i -lt $lastNames.Count; $i++) {
        Save-Tile -Source $booking -Left $lastCells[$i][0] -Top 799 -Right $lastCells[$i][1] -Bottom 970 -Name $lastNames[$i]
    }
}
finally {
    $booking.Dispose()
}

Write-Output "Created $((Get-ChildItem -LiteralPath $OutputDirectory -Filter '*.png').Count) sticker images in $OutputDirectory"
